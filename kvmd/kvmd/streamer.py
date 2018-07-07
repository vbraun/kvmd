import os
import asyncio
import asyncio.subprocess

from typing import List
from typing import Dict
from typing import NamedTuple
from typing import Optional
from typing import Any

import pyudev

from .logging import get_logger

from . import gpio


# =====
class StreamerDeviceInfo(NamedTuple):
    path: str
    bind: str
    driver: str


def explore_device(path: str) -> Optional[StreamerDeviceInfo]:
    # udevadm info -a -p  $(udevadm info -q path -n /dev/sda)
    ctx = pyudev.Context()

    video_device = pyudev.Devices.from_device_file(ctx, path)
    if video_device.subsystem != "video4linux":
        return None
    try:
        if int(video_device.attributes.get("index")) != 0:
            # My strange laptop configuration
            return None
    except ValueError:
        return None

    interface_device = video_device.find_parent("usb", "usb_interface")
    if not interface_device:
        return None

    return StreamerDeviceInfo(
        path=path,
        bind=interface_device.sys_name,
        driver=interface_device.driver,
    )


def locate_by_bind(bind: str) -> str:
    ctx = pyudev.Context()
    for device in ctx.list_devices(subsystem="video4linux"):
        interface_device = device.find_parent("usb", "usb_interface")
        if interface_device and interface_device.sys_name == bind:
            return os.path.join("/dev", device.sys_name)
    return ""


class Streamer:  # pylint: disable=too-many-instance-attributes
    def __init__(
        self,
        cap_power: int,
        conv_power: int,
        bind: str,
        sync_delay: float,
        init_delay: float,
        width: int,
        height: int,
        cmd: List[str],
        loop: asyncio.AbstractEventLoop,
    ) -> None:

        assert cmd, cmd

        self.__cap_power = (gpio.set_output(cap_power) if cap_power > 0 else cap_power)
        self.__conv_power = (gpio.set_output(conv_power) if conv_power > 0 else conv_power)
        self.__bind = bind
        self.__sync_delay = sync_delay
        self.__init_delay = init_delay
        self.__width = width
        self.__height = height
        self.__cmd = cmd

        self.__loop = loop

        self.__proc_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        assert not self.__proc_task
        get_logger().info("Starting streamer ...")
        await self.__set_hw_enabled(True)
        self.__proc_task = self.__loop.create_task(self.__process())

    async def stop(self) -> None:
        assert self.__proc_task
        get_logger().info("Stopping streamer ...")
        self.__proc_task.cancel()
        await asyncio.gather(self.__proc_task, return_exceptions=True)
        await self.__set_hw_enabled(False)
        self.__proc_task = None

    def is_running(self) -> bool:
        return bool(self.__proc_task)

    def get_state(self) -> Dict:
        return {
            "is_running": self.is_running(),
            "size": {
                "width": self.__width,
                "height": self.__height,
            },
        }

    async def cleanup(self) -> None:
        if self.is_running():
            await self.stop()

    async def __set_hw_enabled(self, enabled: bool) -> None:
        # XXX: This sequence is very important to enable converter and cap board
        if self.__cap_power > 0:
            gpio.write(self.__cap_power, enabled)
        if self.__conv_power > 0:
            if enabled:
                await asyncio.sleep(self.__sync_delay)
            gpio.write(self.__conv_power, enabled)
        if enabled:
            await asyncio.sleep(self.__init_delay)

    async def __process(self) -> None:  # pylint: disable=too-many-branches
        logger = get_logger(0)

        while True:  # pylint: disable=too-many-nested-blocks
            proc: Optional[asyncio.subprocess.Process] = None  # pylint: disable=no-member
            try:
                cmd_placeholders: Dict[str, Any] = {"width": self.__width, "height": self.__height}
                if self.__bind:
                    logger.info("Using bind %r as streamer device", self.__bind)
                    device_path = await self.__loop.run_in_executor(None, locate_by_bind, self.__bind)
                    if not device_path:
                        raise RuntimeError("Can't locate device by bind %r" % (self.__bind))
                    cmd_placeholders["device"] = device_path
                cmd = [part.format(**cmd_placeholders) for part in self.__cmd]

                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                logger.info("Started streamer pid=%d: %s", proc.pid, cmd)

                empty = 0
                while proc.returncode is None:
                    line = (await proc.stdout.readline()).decode(errors="ignore").strip()
                    if line:
                        logger.info("streamer: %s", line)
                        empty = 0
                    else:
                        empty += 1
                        if empty == 100:  # asyncio bug
                            break

                raise RuntimeError("WTF")

            except asyncio.CancelledError:
                break

            except Exception as err:
                if proc:
                    logger.exception("Unexpected streamer error: pid=%d", proc.pid)
                else:
                    logger.exception("Can't start streamer: %s", err)
                await asyncio.sleep(1)

            finally:
                if proc and proc.returncode is None:
                    await self.__kill(proc)

    async def __kill(self, proc: asyncio.subprocess.Process) -> None:  # pylint: disable=no-member
        try:
            proc.terminate()
            await asyncio.sleep(1)
            if proc.returncode is None:
                try:
                    proc.kill()
                except Exception:
                    if proc.returncode is not None:
                        raise
            await proc.wait()
            get_logger().info("Streamer killed: pid=%d; retcode=%d", proc.pid, proc.returncode)
        except Exception:
            if proc.returncode is None:
                get_logger().exception("Can't kill streamer pid=%d", proc.pid)
            else:
                get_logger().info("Streamer killed: pid=%d; retcode=%d", proc.pid, proc.returncode)
