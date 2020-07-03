import argparse
from dataclasses import dataclass, field
import itertools
import multiprocessing
import os
import threading
import random
import re
import sys
import time
import traceback
from typing import (
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Tuple,
    Union,
)

from .candidate import CandidateResult
from .compiler import Compiler
from .error import CandidateConstructionFailure
from .net.auth import fetch_servers_and_grant, run_vouch, setup
from .net.client import connect_to_servers
from .net.common import MAX_PRIO, MIN_PRIO
from .permuter import (
    EvalError,
    EvalResult,
    Feedback,
    Finished,
    Message,
    NeedMoreWork,
    Permuter,
    Task,
    WorkDone,
)
from .preprocess import preprocess
from .printer import Printer
from .profiler import Profiler
from .scorer import Scorer

# The probability that the randomizer continues transforming the output it
# generated last time.
DEFAULT_RAND_KEEP_PROB = 0.6


@dataclass
class Options:
    directories: List[str]
    show_errors: bool = False
    show_timings: bool = False
    print_diffs: bool = False
    stack_differences: bool = False
    abort_exceptions: bool = False
    stop_on_zero: bool = False
    keep_prob: float = DEFAULT_RAND_KEEP_PROB
    force_seed: Optional[str] = None
    threads: int = 1
    use_network: bool = False
    network_priority: float = 1.0


def restricted_float(lo: float, hi: float) -> Callable[[str], float]:
    def convert(x: str) -> float:
        try:
            ret = float(x)
        except ValueError:
            raise argparse.ArgumentTypeError(f"invalid float value: '{x}'")

        if ret < lo or ret > hi:
            raise argparse.ArgumentTypeError(
                f"value {x} is out of range (must be between {lo} and {hi})"
            )
        return ret

    return convert


@dataclass
class EvalContext:
    options: Options
    printer: Printer = field(default_factory=Printer)
    iteration: int = 0
    errors: int = 0
    overall_profiler: Profiler = field(default_factory=Profiler)
    permuters: List[Permuter] = field(default_factory=list)


def write_candidate(perm: Permuter, result: CandidateResult) -> None:
    """Write the candidate's C source and score to the next output directory"""
    ctr = 0
    while True:
        ctr += 1
        try:
            output_dir = os.path.join(perm.dir, f"output-{result.score}-{ctr}")
            os.mkdir(output_dir)
            break
        except FileExistsError:
            pass
    source = result.source
    assert source is not None, "_need_to_send_source is wrong!"
    with open(os.path.join(output_dir, "source.c"), "x", encoding="utf-8") as f:
        f.write(source)
    with open(os.path.join(output_dir, "base.c"), "x", encoding="utf-8") as f:
        f.write(perm.base_source)
    with open(os.path.join(output_dir, "score.txt"), "x", encoding="utf-8") as f:
        f.write(f"{result.score}\n")
    with open(os.path.join(output_dir, "diff.txt"), "x", encoding="utf-8") as f:
        f.write(perm.diff(source) + "\n")
    print(f"wrote to {output_dir}")


def post_score(
    context: EvalContext, permuter: Permuter, result: EvalResult, who: Optional[str]
) -> bool:
    if isinstance(result, EvalError):
        context.printer.print(
            "internal permuter failure.", permuter, who, keep_progress=True
        )
        print(result.exc_str)
        if result.seed is not None:
            seed_str = str(result.seed[1])
            if result.seed[0] != 0:
                seed_str = f"{result.seed[0]},{seed_str}"
            print(f"To reproduce the failure, rerun with: --seed {seed_str}")
        if context.options.abort_exceptions:
            sys.exit(1)
        else:
            return False

    if context.options.print_diffs:
        assert result.source is not None, "_need_to_send_source is wrong"
        print()
        print(permuter.diff(result.source))
        input("Press any key to continue...")

    profiler = result.profiler
    score_value = result.score

    context.iteration += 1
    if score_value == permuter.scorer.PENALTY_INF:
        disp_score = "inf"
        context.errors += 1
    else:
        disp_score = str(score_value)
    timings = ""
    if context.options.show_timings:
        for stattype in profiler.time_stats:
            context.overall_profiler.add_stat(stattype, profiler.time_stats[stattype])
        timings = "  \t" + context.overall_profiler.get_str_stats()
    status_line = f"iteration {context.iteration}, {context.errors} errors, score = {disp_score}{timings}"

    if permuter.should_output(result):
        former_best = permuter.best_score
        permuter.record_result(result)
        if score_value < former_best:
            color = "\u001b[32;1m"
            msg = f"found new best score! ({score_value} vs {permuter.base_score})"
        elif score_value == former_best:
            color = "\u001b[32;1m"
            msg = f"tied best score! ({score_value} vs {permuter.base_score})"
        elif score_value < permuter.base_score:
            color = "\u001b[33m"
            msg = f"found a better score! ({score_value} vs {permuter.base_score})"
        else:
            color = "\u001b[33m"
            msg = f"found different asm with same score ({score_value})"
        context.printer.print(msg, permuter, who, color=color)

        write_candidate(permuter, result)
    context.printer.progress(status_line)
    return score_value == 0


def cycle_seeds(permuters: List[Permuter]) -> Iterable[Tuple[int, int]]:
    """
    Return all possible (permuter index, seed) pairs, cycling over permuters.
    If a permuter is randomized, it will keep repeating seeds infinitely.
    """
    iterators: List[Iterator[Tuple[int, int]]] = []
    for perm_ind, permuter in enumerate(permuters):
        it = permuter.seed_iterator()
        iterators.append(zip(itertools.repeat(perm_ind), it))

    i = 0
    while iterators:
        i %= len(iterators)
        item = next(iterators[i], None)
        if item is None:
            del iterators[i]
            i -= 1
        else:
            yield item
            i += 1


def multiprocess_worker(
    permuters: List[Permuter],
    input_queue: "multiprocessing.Queue[Task]",
    output_queue: "multiprocessing.Queue[Feedback]",
) -> None:
    try:
        should_block = False
        while True:
            # Read a work item from the queue. If none is immediately available,
            # tell the main thread to fill the queues more, and then block on
            # the queue.
            queue_item = input_queue.get(block=should_block)
            if queue_item is None:
                output_queue.put((NeedMoreWork(), None))
                should_block = True
                continue
            should_block = False
            if isinstance(queue_item, Finished):
                output_queue.put((queue_item, None))
                output_queue.close()
                break
            permuter_index, seed = queue_item
            permuter = permuters[permuter_index]
            result = permuter.try_eval_candidate(seed)
            if isinstance(result, CandidateResult) and permuter.should_output(result):
                permuter.record_result(result)
            output_queue.put((WorkDone(permuter_index, result), None))
    except KeyboardInterrupt:
        # Don't clutter the output with stack traces; Ctrl+C is the expected
        # way to quit and sends KeyboardInterrupt to all processes.
        # A heartbeat thing here would be good but is too complex.
        # Don't join the queue background thread -- thread joins in relation
        # to KeyboardInterrupt usually result in deadlocks.
        input_queue.cancel_join_thread()
        output_queue.cancel_join_thread()


def run(options: Options) -> List[int]:
    last_time = time.time()
    try:

        def heartbeat() -> None:
            nonlocal last_time
            last_time = time.time()

        return run_inner(options, heartbeat)
    except KeyboardInterrupt:
        if time.time() - last_time > 5:
            print()
            print("Aborting stuck process.")
            traceback.print_exc()
            sys.exit(1)
        print()
        print("Exiting.")
        sys.exit(0)


def run_inner(options: Options, heartbeat: Callable[[], None]) -> List[int]:
    print("Loading...")

    context = EvalContext(options)

    force_seed: Optional[int] = None
    force_rng_seed: Optional[int] = None
    if options.force_seed:
        seed_parts = list(map(int, options.force_seed.split(",")))
        force_rng_seed = seed_parts[-1]
        force_seed = 0 if len(seed_parts) == 1 else seed_parts[0]

    name_counts: Dict[str, int] = {}
    for i, d in enumerate(options.directories):
        heartbeat()
        compile_cmd = os.path.join(d, "compile.sh")
        target_o = os.path.join(d, "target.o")
        base_c = os.path.join(d, "base.c")
        for fname in [compile_cmd, target_o, base_c]:
            if not os.path.isfile(fname):
                print(f"Missing file {fname}", file=sys.stderr)
                sys.exit(1)
        if not os.stat(compile_cmd).st_mode & 0o100:
            print(f"{compile_cmd} must be marked executable.", file=sys.stderr)
            sys.exit(1)

        fn_name: Optional[str] = None
        try:
            with open(os.path.join(d, "function.txt"), encoding="utf-8") as f:
                fn_name = f.read().strip()
        except FileNotFoundError:
            pass

        if fn_name:
            print(f"{base_c} ({fn_name})")
        else:
            print(base_c)

        compiler = Compiler(compile_cmd, show_errors=options.show_errors)
        scorer = Scorer(target_o, stack_differences=options.stack_differences)
        c_source = preprocess(base_c)

        try:
            permuter = Permuter(
                d,
                fn_name,
                compiler,
                scorer,
                base_c,
                c_source,
                force_seed=force_seed,
                force_rng_seed=force_rng_seed,
                keep_prob=options.keep_prob,
                need_all_sources=options.print_diffs,
            )
        except CandidateConstructionFailure as e:
            print(e.message, file=sys.stderr)
            sys.exit(1)

        context.permuters.append(permuter)
        name_counts[permuter.fn_name] = name_counts.get(permuter.fn_name, 0) + 1
    print()

    for permuter in context.permuters:
        if name_counts[permuter.fn_name] > 1:
            permuter.unique_name += f" ({permuter.dir})"
        print(f"[{permuter.unique_name}] base score = {permuter.best_score}")

    found_zero = False
    perm_seed_iter = iter(cycle_seeds(context.permuters))
    if options.threads == 1 and not options.use_network:
        # Simple single-threaded mode. This is not technically needed, but
        # makes the permuter easier to debug.
        for permuter_index, seed in perm_seed_iter:
            heartbeat()
            permuter = context.permuters[permuter_index]
            result = permuter.try_eval_candidate(seed)
            if post_score(context, permuter, result, None):
                found_zero = True
                if options.stop_on_zero:
                    break
    else:
        # Create queues.
        task_queue: "multiprocessing.Queue[Task]" = multiprocessing.Queue()
        feedback_queue: "multiprocessing.Queue[Feedback]" = multiprocessing.Queue()

        # Connect to network and create client threads
        net_threads: List[threading.Thread] = []
        if options.use_network:
            config = setup()
            servers, grant = fetch_servers_and_grant(config)
            net_threads = connect_to_servers(
                config,
                servers,
                grant,
                context.permuters,
                task_queue,
                feedback_queue,
                options.network_priority,
            )

        # Start local worker threads
        processes: List[multiprocessing.Process] = []
        for i in range(options.threads):
            p = multiprocessing.Process(
                target=multiprocess_worker,
                args=(context.permuters, task_queue, feedback_queue),
            )
            p.start()
            processes.append(p)

        active_workers = len(net_threads) + len(processes)

        if not active_workers:
            print("No remote servers available. Exiting.")
            sys.exit(1)

        def process_finish(finish: Finished, who: Optional[str]) -> None:
            nonlocal active_workers

            if finish.reason:
                context.printer.print(finish.reason, None, who, keep_progress=True)
            active_workers -= 1

        def process_result(work: WorkDone, who: Optional[str]) -> bool:
            permuter = context.permuters[work.perm_index]
            return post_score(context, permuter, work.result, who)

        # Feed the task queue with work and read from results queue.
        # We generally match these up one-by-one to avoid overfilling queues,
        # but workers can ask us to add more tasks into the system if they run
        # out of work. (This will happen e.g. at the very beginning, when the
        # queues are empty.)
        while active_workers > 0:
            heartbeat()
            feedback, who = feedback_queue.get()
            if isinstance(feedback, Finished):
                process_finish(feedback, who)
                continue
            if isinstance(feedback, Message):
                context.printer.print(feedback.text, None, who, keep_progress=True)
            elif isinstance(feedback, NeedMoreWork):
                # No result to process, just put a task in the queue.
                pass
            elif process_result(feedback, who):
                # Found score 0!
                found_zero = True
                if options.stop_on_zero:
                    break
            task = next(perm_seed_iter, None)
            if task is None:
                break
            task_queue.put(task)

        # Signal workers to stop.
        for i in range(active_workers):
            task_queue.put(Finished())

        # Await final results.
        while active_workers > 0:
            heartbeat()
            feedback, who = feedback_queue.get()
            if isinstance(feedback, Finished):
                process_finish(feedback, who)
            elif isinstance(feedback, Message):
                context.printer.print(feedback.text, None, who, keep_progress=True)
            elif isinstance(feedback, NeedMoreWork):
                pass
            elif not (options.stop_on_zero and found_zero):
                if process_result(feedback, who):
                    found_zero = True

        # Wait for workers to finish.
        for p in processes:
            p.join()

        for t in net_threads:
            t.join()

    if found_zero:
        print("\nFound zero score! Exiting.")
    return [permuter.best_score for permuter in context.permuters]


def main() -> None:
    multiprocessing.freeze_support()
    sys.setrecursionlimit(10000)

    # Ideally we would do:
    #  multiprocessing.set_start_method('spawn')
    # here, to make multiprocessing behave the same across operating systems.
    # However, that means that arguments to Process are passed across using
    # pickling, which mysteriously breaks with pycparser...
    # (AttributeError: 'CParser' object has no attribute 'p_abstract_declarator_opt')
    # So, for now we live with the defaults, which make multiprocessing work on Linux,
    # where it uses fork and doesn't pickle arguments, and break on Windows. Sigh.

    parser = argparse.ArgumentParser(
        description="Randomly permute C files to better match a target binary."
    )
    parser.add_argument(
        "directories",
        nargs="+",
        metavar="directory",
        help="Directory containing base.c, target.o and compile.sh. Multiple directories may be given.",
    )
    parser.add_argument(
        "--show-errors",
        dest="show_errors",
        action="store_true",
        help="Display compiler error/warning messages, and keep .c files for failed compiles.",
    )
    parser.add_argument(
        "--show-timings",
        dest="show_timings",
        action="store_true",
        help="Display the time taken by permuting vs. compiling vs. scoring.",
    )
    parser.add_argument(
        "--print-diffs",
        dest="print_diffs",
        action="store_true",
        help="Instead of compiling generated sources, display diffs against a base version.",
    )
    parser.add_argument(
        "--abort-exceptions",
        dest="abort_exceptions",
        action="store_true",
        help="Stop execution when an internal permuter exception occurs.",
    )
    parser.add_argument(
        "--stop-on-zero",
        dest="stop_on_zero",
        action="store_true",
        help="Stop after producing an output with score 0.",
    )
    parser.add_argument(
        "--stack-diffs",
        dest="stack_differences",
        action="store_true",
        help="Take stack differences into account when computing the score.",
    )
    parser.add_argument(
        "--keep-prob",
        dest="keep_prob",
        metavar="PROB",
        type=restricted_float(0.0, 1.0),
        default=DEFAULT_RAND_KEEP_PROB,
        help="Continue randomizing the previous output with the given probability "
        f"(float in 0..1, default %(default)s).",
    )
    parser.add_argument("--seed", dest="force_seed", type=str, help=argparse.SUPPRESS)
    parser.add_argument(
        "-j",
        dest="threads",
        type=int,
        default=0,
        help="Number of own threads to use (default: 1 without -J, 0 with -J).",
    )
    parser.add_argument(
        "-J",
        dest="use_network",
        action="store_true",
        help="Harness extra compute power through cyberspace (permuter@home).",
    )
    parser.add_argument(
        "--priority",
        dest="network_priority",
        metavar="PRIORITY",
        type=restricted_float(MIN_PRIO, MAX_PRIO),
        default=1.0,
        help="Proportion of server resources to use when multiple people "
        "are using -J at the same time. "
        "Defaults to 1.0, meaning resources are split equally, but can be "
        f"set to any value within [{MIN_PRIO}, {MAX_PRIO}]. "
        "Each server runs with a priority threshold, which defaults to 0.1, "
        "below which they will not run permuter jobs at all.",
    )
    parser.add_argument(
        "--vouch",
        dest="vouch",
        action="store_true",
        help="Give someone access to the permuter@home server.",
    )

    args = parser.parse_args()

    threads = args.threads
    if not threads and not args.use_network:
        threads = 1

    if args.vouch:
        run_vouch(args.directories[0])
        return

    options = Options(
        directories=args.directories,
        show_errors=args.show_errors,
        show_timings=args.show_timings,
        print_diffs=args.print_diffs,
        abort_exceptions=args.abort_exceptions,
        stack_differences=args.stack_differences,
        stop_on_zero=args.stop_on_zero,
        keep_prob=args.keep_prob,
        force_seed=args.force_seed,
        threads=threads,
        use_network=args.use_network,
        network_priority=args.network_priority,
    )

    run(options)


if __name__ == "__main__":
    main()
