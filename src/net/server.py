import abc
import base64
from dataclasses import dataclass, field
from enum import Enum
import json
import os
import pathlib
import queue
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
import traceback
from typing import BinaryIO, Dict, List, Optional, Set, Tuple, Union
import uuid
import zlib

import docker
from nacl.public import Box, PrivateKey, PublicKey
from nacl.secret import SecretBox
from nacl.signing import SigningKey, VerifyKey
import nacl.utils

from ..helpers import exception_to_string, static_assert_unreachable
from .core import (
    PermuterData,
    Port,
    SocketPort,
    file_read_fixed,
    json_prop,
    permuter_data_from_json,
    permuter_data_to_json,
)


@dataclass
class Client:
    id: str
    nickname: str


class InitState(Enum):
    UNINIT = 0
    WAITING = 1
    READY = 2


@dataclass
class ClientState:
    handle: int
    client: Client
    init_state: InitState = InitState.UNINIT


@dataclass
class AddPermuter:
    handle: int
    client: Client
    permuter_data: PermuterData


@dataclass
class RemovePermuter:
    handle: int


@dataclass
class Work:
    handle: int
    seed: int


@dataclass
class ImmediateDisconnect:
    handle: int
    client: Client
    reason: str


@dataclass
class Disconnect:
    handle: int


@dataclass
class PermInitFail:
    perm_id: str
    error: str


@dataclass
class PermInitSuccess:
    perm_id: str
    base_score: int
    base_hash: str
    time_us: float


@dataclass
class WorkDone:
    perm_id: str
    obj: dict
    time_us: float
    compressed_source: Optional[bytes]


class NeedMoreWork:
    pass


@dataclass
class NetThreadDisconnected:
    thread_id: int


class Heartbeat:
    pass


class Shutdown:
    pass


Activity = Union[
    AddPermuter,
    RemovePermuter,
    Work,
    ImmediateDisconnect,
    Disconnect,
    PermInitFail,
    PermInitSuccess,
    WorkDone,
    NeedMoreWork,
    NetThreadDisconnected,
    Heartbeat,
    Shutdown,
]


@dataclass
class OutputInitFail:
    handle: int
    error: str


@dataclass
class OutputInitSuccess:
    handle: int
    base_score: int
    base_hash: str
    time_us: float


@dataclass
class OutputDisconnect:
    handle: int


@dataclass
class OutputNeedMoreWork:
    pass


@dataclass
class OutputWork:
    handle: int
    time_us: float
    obj: dict
    compressed_source: Optional[bytes]


Output = Union[
    OutputDisconnect,
    OutputInitFail,
    OutputInitSuccess,
    OutputNeedMoreWork,
    OutputWork,
    Shutdown,
]


@dataclass
class IoConnect:
    fn_name: str
    client: Client


@dataclass
class IoDisconnect:
    reason: str


@dataclass
class IoImmediateDisconnect:
    reason: str
    client: Client


class IoShutdown:
    pass


class IoWillSleep:
    pass


@dataclass
class IoWorkDone:
    score: Optional[int]
    is_improvement: bool


IoMessage = Union[IoConnect, IoDisconnect, IoImmediateDisconnect, IoWorkDone]
IoGlobalMessage = Union[IoShutdown, IoWillSleep]
IoActivity = Union[Tuple[int, IoMessage], IoGlobalMessage]


@dataclass
class ServerOptions:
    num_cores: float
    max_memory_gb: float
    min_priority: float
    systray: bool


class NetThread:
    thread_id: int
    _port: SocketPort
    _main_queue: "queue.Queue[Activity]"
    _controller_queue: "queue.Queue[Output]"
    _read_thread: "threading.Thread"
    _write_thread: "threading.Thread"

    def __init__(
        self,
        thread_id: int,
        port: SocketPort,
        main_queue: "queue.Queue[Activity]",
    ) -> None:
        self.thread_id = thread_id
        self._port = port
        self._main_queue = main_queue
        self._controller_queue = queue.Queue()

        self._read_thread = threading.Thread(target=self.read_loop)
        self._read_thread.daemon = True
        self._read_thread.start()

        self._write_thread = threading.Thread(target=self.write_loop)
        self._write_thread.daemon = True
        self._write_thread.start()

    def stop(self) -> None:
        self._controller_queue.put(Shutdown())
        self._port.shutdown()
        self._read_thread.join()
        self._write_thread.join()

    def send_controller(self, msg: Output) -> None:
        self._controller_queue.put(msg)

    def _read_one(self) -> Activity:
        msg = self._port.receive_json()

        msg_type = json_prop(msg, "type", str)

        if msg_type == "heartbeat":
            return Heartbeat()

        handle = json_prop(msg, "permuter", int)

        if msg_type == "work":
            seed = json_prop(msg, "seed", int)
            return Work(handle=handle, seed=seed)

        elif msg_type == "add":
            client_id = json_prop(msg, "client_id", str)
            client_name = json_prop(msg, "client_name", str)
            client = Client(client_id, client_name)
            data = json_prop(msg, "data", dict)
            compressed_source = self._port.receive()
            compressed_target_o_bin = self._port.receive()

            try:
                source = zlib.decompress(compressed_source).decode("utf-8")
                target_o_bin = zlib.decompress(compressed_target_o_bin)
                permuter = permuter_data_from_json(data, source, target_o_bin)
            except Exception as e:
                # Client sent something illegible. This can legitimately happen if the
                # client runs another version, but it's interesting to log.
                traceback.print_exc()
                return ImmediateDisconnect(
                    handle=handle,
                    client=client,
                    reason=f"Failed to parse permuter: {exception_to_string(e)}",
                )

            return AddPermuter(
                handle=handle,
                client=client,
                permuter_data=permuter,
            )

        elif msg_type == "remove":
            return RemovePermuter(handle=handle)

        else:
            raise Exception(f"Bad message type: {msg_type}")

    def read_loop(self) -> None:
        try:
            while True:
                msg = self._read_one()
                self._main_queue.put(msg)
        except Exception as e:
            if not isinstance(e, EOFError):
                traceback.print_exc()
            self._main_queue.put(NetThreadDisconnected(self.thread_id))

    def _write_one(self, item: Output) -> None:
        if isinstance(item, Shutdown):
            # Handled by caller
            pass

        elif isinstance(item, OutputInitFail):
            self._port.send_json(
                {
                    "type": "update",
                    "permuter": item.handle,
                    "time_us": 0,
                    "update": {"type": "init_failed", "reason": item.error},
                }
            )

        elif isinstance(item, OutputInitSuccess):
            self._port.send_json(
                {
                    "type": "update",
                    "permuter": item.handle,
                    "time_us": item.time_us,
                    "update": {"type": "init_done", "hash": item.base_hash},
                }
            )

        elif isinstance(item, OutputDisconnect):
            self._port.send_json(
                {
                    "type": "update",
                    "permuter": item.handle,
                    "time_us": 0,
                    "update": {"type": "disconnect"},
                }
            )

        elif isinstance(item, OutputNeedMoreWork):
            self._port.send_json({"type": "need_work"})

        elif isinstance(item, OutputWork):
            self._port.send_json(
                {
                    "type": "update",
                    "permuter": item.handle,
                    "time_us": item.time_us,
                    "update": {
                        "type": "work",
                        **item.obj,
                    },
                }
            )
            if item.compressed_source is not None:
                self._port.send(item.compressed_source)

        else:
            static_assert_unreachable(item)

    def write_loop(self) -> None:
        try:
            while True:
                item = self._controller_queue.get()
                if isinstance(item, Shutdown):
                    break
                self._write_one(item)
        except Exception as e:
            if not isinstance(e, EOFError):
                traceback.print_exc()
            self._main_queue.put(NetThreadDisconnected(self.thread_id))


class Server:
    _options: ServerOptions
    _net_port: SocketPort
    _evaluator_port: Port
    _main_queue: "queue.Queue[Activity]"
    _io_queue: "queue.Queue[IoActivity]"
    _state: str
    _net_thread: Optional[NetThread]
    _next_net_thread_id: int
    _active: Set[int]

    def __init__(
        self,
        net_port: SocketPort,
        options: ServerOptions,
        evaluator_port: Port,
        io_queue: "queue.Queue[IoActivity]",
    ) -> None:
        self._options = options
        self._net_port = net_port
        self._evaluator_port = evaluator_port
        self._main_queue = queue.Queue()
        self._io_queue = io_queue
        self._state = "notstarted"
        self._net_thread = None
        self._next_net_thread_id = 0
        self._active = set()

    def _send_controller(self, msg: Output) -> None:
        if self._net_thread:
            self._net_thread.send_controller(msg)

    def _send_io(self, handle: int, io_msg: IoMessage) -> None:
        self._io_queue.put((handle, io_msg))

    def _send_io_global(self, io_msg: IoGlobalMessage) -> None:
        self._io_queue.put(io_msg)

    def _handle_message(self, msg: Activity) -> None:
        if isinstance(msg, Shutdown):
            # Handled by caller
            pass

        elif isinstance(msg, Heartbeat):
            pass

        elif isinstance(msg, Work):
            if msg.handle not in self._active:
                self._need_work()
                return

            self._evaluator_port.send_json(
                {
                    "type": "work",
                    "id": str(msg.handle),
                    "seed": msg.seed,
                }
            )

        elif isinstance(msg, AddPermuter):
            if msg.handle in self._active:
                raise Exception("Repeated AddPermuter!")

            self._active.add(msg.handle)
            self._send_permuter(str(msg.handle), msg.permuter_data)
            fn_name = msg.permuter_data.fn_name
            self._send_io(msg.handle, IoConnect(fn_name, msg.client))

        elif isinstance(msg, RemovePermuter):
            if msg.handle not in self._active:
                return

            self._remove(msg.handle)
            self._send_io(msg.handle, IoDisconnect("disconnected"))

        elif isinstance(msg, Disconnect):
            if msg.handle not in self._active:
                return

            self._remove(msg.handle)
            self._send_io(msg.handle, IoDisconnect("kicked"))
            self._send_controller(OutputDisconnect(handle=msg.handle))

        elif isinstance(msg, ImmediateDisconnect):
            if msg.handle in self._active:
                raise Exception("ImmediateDisconnect is not immediate")

            self._send_io(
                msg.handle, IoImmediateDisconnect("sent garbage message", msg.client)
            )
            self._send_controller(OutputDisconnect(handle=msg.handle))

        elif isinstance(msg, PermInitFail):
            handle = int(msg.perm_id)
            if handle not in self._active:
                self._need_work()
                return

            self._active.remove(handle)
            self._send_io(handle, IoDisconnect("failed to compile"))
            self._send_controller(
                OutputInitFail(
                    handle=handle,
                    error=msg.error,
                )
            )

        elif isinstance(msg, PermInitSuccess):
            handle = int(msg.perm_id)
            if handle not in self._active:
                self._need_work()
                return

            self._send_controller(
                OutputInitSuccess(
                    handle=handle,
                    time_us=msg.time_us,
                    base_score=msg.base_score,
                    base_hash=msg.base_hash,
                )
            )

        elif isinstance(msg, WorkDone):
            handle = int(msg.perm_id)
            if handle not in self._active:
                self._need_work()
                return

            obj = msg.obj
            obj["permuter"] = handle
            score = json_prop(obj, "score", int) if "score" in obj else None
            is_improvement = msg.compressed_source is not None
            self._send_io(
                handle,
                IoWorkDone(score=score, is_improvement=is_improvement),
            )
            self._send_controller(
                OutputWork(
                    handle=handle,
                    time_us=msg.time_us,
                    obj=obj,
                    compressed_source=msg.compressed_source,
                )
            )

        elif isinstance(msg, NeedMoreWork):
            self._need_work()

        elif isinstance(msg, NetThreadDisconnected):
            if self._net_thread is None or msg.thread_id != self._net_thread.thread_id:
                return

            for handle in list(self._active):
                self._remove(handle)

            print("disconnected from permuter@home")

            self._stop_net_thread()
            # TODO reconnect after a while
            # (requires another mapping of new permuter ids to evaluator ones
            # than stringification to avoid collisions)

        else:
            static_assert_unreachable(msg)

    def _need_work(self) -> None:
        self._send_controller(OutputNeedMoreWork())

    def _remove(self, handle: int) -> None:
        self._evaluator_port.send_json({"type": "remove", "id": str(handle)})
        self._active.remove(handle)

    def _send_permuter(self, id: str, perm: PermuterData) -> None:
        self._evaluator_port.send_json(
            {"type": "add", "id": id, **permuter_data_to_json(perm)}
        )
        self._evaluator_port.send(perm.source.encode("utf-8"))
        self._evaluator_port.send(perm.target_o_bin)

    def _do_read_eval_loop(self) -> None:
        while True:
            msg = self._evaluator_port.receive_json()
            msg_type = json_prop(msg, "type", str)

            if msg_type == "init":
                perm_id = json_prop(msg, "id", str)
                time_us = json_prop(msg, "time_us", float)
                resp: Activity
                if json_prop(msg, "success", bool):
                    resp = PermInitSuccess(
                        perm_id=perm_id,
                        base_score=json_prop(msg, "base_score", int),
                        base_hash=json_prop(msg, "base_hash", str),
                        time_us=time_us,
                    )
                else:
                    resp = PermInitFail(
                        perm_id=perm_id,
                        error=json_prop(msg, "error", str),
                    )
                self._main_queue.put(resp)

            elif msg_type == "result":
                compressed_source: Optional[bytes] = None
                if msg.get("has_source") == True:
                    compressed_source = self._evaluator_port.receive()
                perm_id = json_prop(msg, "id", str)
                time_us = json_prop(msg, "time_us", float)
                del msg["id"]
                del msg["time_us"]
                self._main_queue.put(
                    WorkDone(
                        perm_id=perm_id,
                        obj=msg,
                        time_us=time_us,
                        compressed_source=compressed_source,
                    )
                )

            elif msg_type == "need_work":
                self._main_queue.put(NeedMoreWork())

            else:
                raise Exception(f"Unknown message type from evaluator: {msg_type}")

    def _read_eval_loop(self) -> None:
        try:
            self._do_read_eval_loop()
        except EOFError:
            # Silence errors from shutdown.
            pass

    def _main_loop(self) -> None:
        while True:
            msg = self._main_queue.get()
            if isinstance(msg, Shutdown):
                break

            self._handle_message(msg)

            if not self._active and self._main_queue.empty():
                self._send_io_global(IoWillSleep())

    def start(self) -> None:
        assert self._state == "notstarted"
        self._state = "started"

        self._net_thread = NetThread(
            self._next_net_thread_id, self._net_port, self._main_queue
        )
        self._next_net_thread_id += 1

        # Start a thread for reading evaluator results and sending them on to
        # the main loop queue.
        read_eval_thread = threading.Thread(target=self._read_eval_loop)
        read_eval_thread.daemon = True
        read_eval_thread.start()

        # Start a thread for the main loop.
        main_thread = threading.Thread(target=self._main_loop)
        main_thread.daemon = True
        main_thread.start()

    def remove_permuter(self, handle: int) -> None:
        self._main_queue.put(Disconnect(handle=handle))

    def _stop_net_thread(self) -> None:
        if self._net_thread is None:
            return
        self._net_thread.stop()
        self._net_thread = None

    def stop(self) -> None:
        assert self._state == "started"
        self._state = "finished"
        self._main_queue.put(Shutdown())
        self._stop_net_thread()


class DockerPort(Port):
    """Port for communicating with Docker. Communication is encrypted for a few
    not-very-good reasons:
    - it allows code reuse
    - it adds error-checking
    - it was fun to implement"""

    _sock: BinaryIO
    _container: docker.models.containers.Container
    _stdout_buffer: bytes

    def __init__(
        self, container: docker.models.containers.Container, secret: bytes
    ) -> None:
        self._container = container
        self._stdout_buffer = b""

        # Set up a socket for reading from stdout/stderr and writing to
        # stdin for the container. The docker package does not seem to
        # expose an API for writing the stdin, but we can do so directly
        # by attaching a socket and poking at internal state. (See
        # https://github.com/docker/docker-py/issues/983.) For stdout/
        # stderr, we use the format described at
        # https://docs.docker.com/engine/api/v1.24/#attach-to-a-container.
        #
        # Hopefully this will keep working for at least a while...
        try:
            self._sock = container.attach_socket(
                params={"stdout": True, "stdin": True, "stderr": True, "stream": True}
            )
            self._sock._writing = True  # type: ignore
        except:
            try:
                container.remove(force=True)
            except Exception:
                pass
            raise

        super().__init__(SecretBox(secret), "docker", is_client=True)

    def shutdown(self) -> None:
        try:
            self._sock.close()
            self._container.remove(force=True)
        except Exception as e:
            print("Failed to shut down Docker")
            traceback.print_exc()

    def _read_one(self) -> None:
        header = file_read_fixed(self._sock, 8)
        stream, length = struct.unpack(">BxxxI", header)
        if stream not in [1, 2]:
            raise Exception("Unexpected output from Docker: " + repr(header))
        data = file_read_fixed(self._sock, length)
        if stream == 1:
            self._stdout_buffer += data
        else:
            sys.stderr.buffer.write(b"Docker stderr: " + data)
            sys.stderr.buffer.flush()

    def _receive(self, length: int) -> bytes:
        while len(self._stdout_buffer) < length:
            self._read_one()
        ret = self._stdout_buffer[:length]
        self._stdout_buffer = self._stdout_buffer[length:]
        return ret

    def _send(self, data: bytes) -> None:
        while data:
            written = self._sock.write(data)
            data = data[written:]
        self._sock.flush()


def start_evaluator(docker_image: str, options: ServerOptions) -> DockerPort:
    """Spawn a docker container and set it up to evaluate permutations in,
    returning a handle that we can use to communicate with it.

    We do this for a few reasons:
    - enforcing a known Linux environment, all while the outside server can run
      on e.g. Windows and display a systray
    - enforcing resource limits
    - sandboxing

    Docker does have the downside of requiring root access, so ideally we would
    also have a Docker-less mode, where we leave the sandboxing to some other
    tool, e.g. https://github.com/ioi/isolate/."""
    print("Starting docker...")
    command = ["python3", "-m", "src.net.evaluator"]
    secret = nacl.utils.random(32)
    box = SecretBox(secret)
    enc_secret = base64.b64encode(secret).decode("utf-8")
    src_path = pathlib.Path(__file__).parent.parent.absolute()

    try:
        client = docker.from_env()
        client.info()
    except Exception:
        print(
            "Failed to start docker. Make sure you have docker installed, "
            "and either run the permuter with sudo or add yourself to the "
            '"docker" UNIX group.'
        )
        sys.exit(1)

    try:
        container = client.containers.run(
            docker_image,
            command,
            detach=True,
            remove=True,
            stdin_open=True,
            stdout=True,
            environment={"SECRET": enc_secret},
            volumes={src_path: {"bind": "/src", "mode": "ro"}},
            tmpfs={"/tmp": "size=1G,exec"},
            nano_cpus=int(options.num_cores * 1e9),
            mem_limit=int(options.max_memory_gb * 2 ** 30),
            read_only=True,
            network_disabled=True,
        )
    except Exception as e:
        print(f"Failed to start docker container: {e}")
        sys.exit(1)

    port = DockerPort(container, secret)

    try:
        # Sanity-check that the Docker container started successfully and can
        # be communicated with.
        magic = b"\0" * 1000
        port.send(magic)
        r = port.receive()
        if r != magic:
            raise Exception("Failed initial sanity check.")

        port.send_json({"num_cores": options.num_cores})
    except:
        port.shutdown()
        raise

    print("Started.")
    return port
