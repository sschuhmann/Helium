"""Definition of KernelConnection class.

KernelConnection class provides interaction with Jupyter kernels.

Copyright (c) 2017-2018, NEGORO Tetsuya (https://github.com/ngr-t)
"""
import re
from collections import defaultdict
from datetime import datetime
from queue import Empty, Queue
from threading import Event, RLock, Thread
from typing import Dict, List, Optional, Tuple
from uuid import UUID

import sublime
from enum import Enum, unique

from .utils import show_password_input

JUPYTER_PROTOCOL_VERSION = "5.0"


@unique
class MsgType(Enum):
    """Representation of Message type."""

    # TODO: use auto, once on ST4
    EXECUTE_INPUT = 0
    EXECUTE_REQUEST = 1
    EXECUTE_RESULT = 2
    EXECUTE_REPLY = 3
    COMPLETE_REQUEST = 4
    COMPLETE_REPLY = 5
    DISPLAY_DATA = 6
    INSPECT_REQUEST = 7
    INSPECT_REPLY = 8
    INPUT_REQUEST = 9
    INPUT_REPLY = 10
    ERROR = 11
    STREAM = 12
    STATUS = 13
    UNKNOWN = 14


@unique
class ExecState(Enum):
    """Representation of execution states.

    https://jupyter-client.readthedocs.io/en/stable/messaging.html#kernel-status
    """

    # TODO: use auto, once on ST4
    BUSY = 0
    IDLE = 1
    STARTING = 2
    # `UNKNOWN` is a custom state not in the protocol specification
    UNKNOWN = 3


HELIUM_FIGURE_PHANTOMS = "helium_figure_phantoms"

# Used as key of status bar.
KERNEL_STATUS_KEY = "helium_kernel_status"

HELIUM_OBJECT_INSPECT_PANEL = "helium_object_inspect"

ANSI_ESCAPE_PATTERN = re.compile(r"\x1b[^m]*m")

OUTPUT_VIEW_SEPARATOR = "-" * 80

TEXT_PHANTOM = """<body id="helium-result">
  <style>
    .stdout {{ color: color(var(--foreground) alpha(0.7)) }}
    .error {{ color: var(--redish) }}
    .other {{ color: var(--yellowish) }}
    .closebutton {{ text-decoration: none }}
  </style>
  <a class=closebutton href=hide>×</a>
  {content}
</body>"""

IMAGE_PHANTOM = """<body id="helium-image-result" style="background-color:white">
  <style>
    .image {{ background-color: white }}
    .closebutton {{ text-decoration: none }}
  </style>
  <a class=closebutton href=hide>×</a>
  <br>
  <img class="image" alt="Out" src="data:image/png;base64,{data}" />
</body>"""

STREAM_PHANTOM = "<div class={name}>{content}</div>"


def fix_whitespace_for_phantom(text: str):  # type: (...) -> str
    """Transform output for proper display.

    This is important to display pandas DataFrames, for instance.
    """
    text = text.replace(" ", r"&nbsp;")
    text = "<br>".join(text.splitlines())
    return text


def extract_content(messages, msg_type: MsgType):  # type: (...) -> List[str]
    """Extract content from messages received from a kernel."""
    return [
        message["content"]
        for message in messages
        # TODO: Check this comparison works
        if message["header"]["msg_type"] == msg_type
    ]


def remove_ansi_escape(text: str):  # type: (...) -> str
    return ANSI_ESCAPE_PATTERN.sub("", text)


def get_msg_type(message):
    # TODO: Check whether this should be str or respective Enum
    return message["header"]["msg_type"]


def extract_data(result):  # type: (...) -> str
    """Extract plain text data."""
    try:
        return result["data"]
    except KeyError:
        return ""


class KernelConnection(object):
    """Interact with a Jupyter kernel."""

    class MessageReceiver(Thread):  # noqa
        def __init__(self, kernel):  # type: (...) -> None
            """Initialize AsyncCommunicator class."""
            super().__init__()
            self._kernel = kernel
            self.exit = Event()

        def shutdown(self):  # type: (...) -> None
            self.exit.set()

    class ShellMessageReceiver(MessageReceiver):
        """Communicator that runs asynchroniously."""

        def run(self):  # type: (...) -> None
            """Run main routine."""
            # TODO: implement logging
            # TODO: remove view and regions from id2region
            while not self.exit.is_set():
                try:
                    msg = self._kernel.client.get_shell_msg(timeout=1)
                    self._kernel.shell_msg_queues_lock.acquire()
                    try:
                        queue = self._kernel.shell_msg_queues[
                            msg["parent_header"]["msg_id"]
                        ]
                    finally:
                        self._kernel.shell_msg_queues_lock.release()
                    queue.put(msg)
                except Empty:  # TODO: Make more descriptive Exception name
                    pass
                except Exception as ex:
                    self._kernel._logger.exception(ex)

    class IOPubMessageReceiver(MessageReceiver):
        """Receive and process IOPub messages."""

        def run(self):  # type: (...) -> None
            """Run main routine."""
            # TODO: log, handle other message types.
            while not self.exit.is_set():
                try:
                    msg = self._kernel.client.get_iopub_msg(timeout=1)
                    self._kernel._logger.info(msg)
                    content = msg.get("content", {})
                    execution_count = content.get("execution_count", None)
                    try:
                        msg_type = MsgType[msg["msg_type"].upper()]
                    except KeyError:
                        msg_type = MsgType.UNKNOWN

                    view, region = self._kernel.id2region.get(
                        msg["parent_header"].get("msg_id", None), (None, None)
                    )

                    self._kernel._logger.info(msg_type)
                    if msg_type is MsgType.STATUS:
                        state = ExecState[content["execution_state"].upper()]
                        self._kernel._execution_state = state
                    elif msg_type is MsgType.EXECUTE_INPUT:
                        self._kernel._write_text_to_view("\n\n")
                        self._kernel._output_input_code(
                            content["code"], content["execution_count"]
                        )
                    elif msg_type is MsgType.ERROR:
                        self._kernel._logger.info("Handling error")
                        self._kernel._handle_error(
                            execution_count,
                            content["ename"],
                            content["evalue"],
                            content["traceback"],
                            region,
                            view
                        )
                    elif msg_type is MsgType.DISPLAY_DATA:
                        self._kernel._write_mime_data_to_view(
                            content["data"], region, view
                        )
                    elif msg_type is MsgType.EXECUTE_RESULT:
                        self._kernel._write_mime_data_to_view(
                            content["data"], region, view
                        )
                    elif msg_type is MsgType.STREAM:
                        self._kernel._handle_stream(
                            content["name"], content["text"], region, view,
                        )
                except Empty:
                    pass
                except Exception as ex:  # TODO: Make exception more stringent
                    self._kernel._logger.exception(ex)

    class StdInMessageReceiver(MessageReceiver):
        """Receive and process IOPub messages."""

        def _handle_input_request(self, prompt: str, password: str):
            # type: (...) -> None
            def interrupt():
                self._kernel.interrupt_kernel(self.kernel_id)

            if password:
                show_password_input(prompt, self._kernel.input, interrupt)
            else:
                (
                    sublime.active_window().show_input_panel(
                        prompt, "", self._kernel.client.input, lambda x: None, interrupt
                    )
                )

        def run(self):  # type: (...) -> None
            """Run main routine."""
            # TODO: log, handle other message types.
            while not self.exit.is_set():
                try:
                    msg = self._kernel.client.get_stdin_msg(timeout=1)
                    msg_type = msg["msg_type"]
                    content = msg["content"]
                    if msg_type == MsgType.INPUT_REQUEST:
                        self._handle_input_request(
                            content["prompt"], content["password"]
                        )
                except Empty:
                    pass
                except Exception as ex:
                    self._kernel._logger.exception(ex)

    def _init_receivers(self):  # type: (...) -> None
        # Set the attributes refered by receivers before they start.
        self._shell_msg_receiver = self.ShellMessageReceiver(self)
        self._shell_msg_receiver.start()
        self._iopub_msg_receiver = self.IOPubMessageReceiver(self)
        self._iopub_msg_receiver.start()
        self._stdin_msg_receiver = self.StdInMessageReceiver(self)
        self._stdin_msg_receiver.start()

    def __init__(
        self,
        kernel_id: UUID,
        kernel_manager,  # TODO: This might be KernelManager,
        parent,
        connection_name=None,
        logger=None
    ):
        """Initialize KernelConnection class.

        paramters
        ---------
        kernel_id str: kernel ID
        parent parent kernel manager
        """
        self._logger = logger
        self.shell_msg_queues = defaultdict(Queue)  # tpye: DefaultDict
        self._kernel_id = kernel_id
        self.parent = parent
        self.kernel_manager = kernel_manager
        self.client = self.kernel_manager.client()
        self.client.start_channels()
        self.shell_msg_queues_lock = RLock()
        self.id2region = {}
        self._connection_name = connection_name
        self._execution_state = ExecState.UNKNOWN
        self._init_receivers()

    def __del__(self):  # noqa
        self._shell_msg_receiver.shutdown()
        self._iopub_msg_receiver.shutdown()
        self._stdin_msg_receiver.shutdown()

    @property
    def lang(self):
        """Language of kernel."""
        return self.kernel_manager.kernel_name

    @property
    def kernel_id(self):  # type: (...) -> UUID
        """ID of kernel."""
        return self._kernel_id

    def shutdown_kernel(self):  # type: (...) -> None
        self.kernel_manager.shutdown_kernel()

    def restart_kernel(self):  # type: (...) -> None
        self.kernel_manager.restart_kernel()

    def interrupt_kernel(self):  # type: (...) -> None
        self.kernel_manager.interrupt_kernel()

    def get_connection_name(self):  # type: (...) -> str
        return self._connection_name

    def set_connection_name(self, new_name: str):  # type: (...) -> None
        # We also have to change the view name now.
        view = self.get_view()
        self._connection_name = new_name
        view.set_name(self.view_name)

    def del_connection_name(self):  # type: (...) -> None
        self._connection_name = None

    connection_name = property(
        get_connection_name,
        set_connection_name,
        del_connection_name,
        "Name of kernel connection shown in a view title."
    )

    @property
    def view_name(self):  # type: (...) -> str
        """Return name of output view."""
        return "*Helium Output* {repr}".format(repr=self.repr)

    @property
    def repr(self):  # type: (...) -> str
        """Return string representation of the connection."""
        if self.connection_name:
            return "{connection_name} ([{lang}] {kernel_id})".format(
                connection_name=self.connection_name,
                lang=self.lang,
                kernel_id=self.kernel_id
            )
        else:
            return "[{lang}] {kernel_id}".format(
                lang=self.lang, kernel_id=self.kernel_id
            )

    @property
    def execution_state(self):  # type: (...) -> ExecState
        return self._execution_state

    @property
    def _show_inline_output(self):  # type: (...) -> bool  # TODO: Rename to _should..
        return sublime.load_settings("Helium.sublime-settings").get("inline_output")

    def activate_view(self):  # type: (...) -> None
        """Activate view to show the output of kernel."""
        view = self.get_view()
        current_view = sublime.active_window().active_view()
        sublime.active_window().focus_view(view)
        view.set_scratch(True)  # avoids prompting to save
        view.settings().set("word_wrap", "false")
        sublime.active_window().focus_view(current_view)

    def _output_input_code(self, code: str, execution_count: int):
        # type: (...) -> None
        line = "In[{execution_count}]: {code}".format(
            execution_count=execution_count, code=code
        )
        self._write_text_to_view(line)

    def _handle_error(
        self,
        execution_count: int,
        ename: str,
        evalue: str,
        traceback: str,
        region: sublime.Region = None,
        view: sublime.View = None
    ):  # type: (...) -> None
        try:
            lines = "\nError[{execution_count}]: {ename}, {evalue}."
            "\nTraceback:\n{traceback}".format(
                execution_count=execution_count,
                ename=ename,
                evalue=evalue,
                traceback="\n".join(traceback)
            )
            lines = remove_ansi_escape(lines)
            self._write_text_to_view(lines)
            if region is not None:
                phantom_html = STREAM_PHANTOM.format(
                    name="error", content=fix_whitespace_for_phantom(lines)
                )
                self._write_inline_html_phantom(phantom_html, region, view)
        except AttributeError:
            # Just there is no error.
            pass

    def _handle_stream(
        self,
        name: str,
        text: str,
        region: sublime.Region = None,
        view: sublime.View = None
    ):  # type: (...) -> None
        # Currently don't consider real time catching of streams.
        try:
            lines = "\n({name}):\n{text}".format(name=name, text=text)
            phantom_html = STREAM_PHANTOM.format(
                name=name, content=fix_whitespace_for_phantom(text)
            )
            self._write_text_to_view(lines)
            if phantom_html and (region is not None):
                self._write_inline_html_phantom(phantom_html, region, view)
        except AttributeError:
            # Just there is no error.
            pass

    def _write_out_execution_count(self, execution_count: int):  # type: (...) -> None
        self._write_text_to_view("\nOut[{}]: ".format(execution_count))

    def _write_text_to_view(self, text: str):  # type: (...) -> None
        if self._show_inline_output:
            return
        self.activate_view()
        view = self.get_view()
        view.set_read_only(False)
        view.run_command("append", {"characters": text})
        view.set_read_only(True)
        view.show(view.size())

    def _write_phantom(self, content: str):  # type: (...) -> None
        if self._show_inline_output:
            return
        self.activate_view()
        file_size = self.get_view().size()
        region = sublime.Region(file_size, file_size)
        self.get_view().add_phantom(
            HELIUM_FIGURE_PHANTOMS, region, content, sublime.LAYOUT_BLOCK
        )
        self._logger.info("Created phantom {}".format(content))

    def _write_inline_html_phantom(
        self, content: str, region: sublime.Region, view: sublime.View
    ):  # type: (...) -> None
        if self._show_inline_output:
            id = HELIUM_FIGURE_PHANTOMS + datetime.now().isoformat()
            html = TEXT_PHANTOM.format(content=content)
            view.add_phantom(
                id,
                region,
                html,
                sublime.LAYOUT_BLOCK,
                on_navigate=lambda href, id=id: view.erase_phantoms(id),
            )
            self._logger.info("Created inline phantom {}".format(html))

    def _write_inline_image_phantom(
        self, data: str, region: sublime.Region, view: sublime.View
    ):  # type: (...) -> None
        if self._show_inline_output:
            id = HELIUM_FIGURE_PHANTOMS + datetime.now().isoformat()
            html = IMAGE_PHANTOM.format(data=data)
            view.add_phantom(
                id,
                region,
                html,
                sublime.LAYOUT_BLOCK,
                on_navigate=lambda href, id=id: view.erase_phantoms(id),
            )
            self._logger.info("Created inline phantom image")

    def _write_mime_data_to_view(
        self, mime_data: dict, region: sublime.Region, view: sublime.View
    ):  # type: (...) -> None
        # Now we use basically text/plain for text type.
        # Jupyter kernels often emits html whom minihtml cannot render.
        if "text/plain" in mime_data:
            content = mime_data["text/plain"]
            lines = "\n(display data): {content}".format(content=content)
            self._write_text_to_view(lines)
            self._write_inline_html_phantom(
                fix_whitespace_for_phantom(content), region, view
            )
        elif "text/html" in mime_data:
            self._logger.info(
                "Caught 'text/html' output without plain text. "
                "Try to show with phantom."
            )
            content = mime_data["text/html"]
            self._write_phantom(content)
            self._write_inline_html_phantom(content, region, view)

        if "image/png" in mime_data:
            data = mime_data["image/png"].strip()
            self._logger.info("Caught image.")
            self._logger.info("RELOADED -------------=================")
            content = (
                '<body style="background-color:white">'
                + '<img alt="Out" src="data:image/png;base64,{data}" />'
                + "</body>"
            ).format(data=data, bgcolor="white")
            self._write_phantom(content)
            self._write_inline_image_phantom(data, region, view)

    def _handle_inspect_reply(self, reply: Dict[str, str]):  # type: (...) -> None
        window = sublime.active_window()
        if window.find_output_panel(HELIUM_OBJECT_INSPECT_PANEL) is not None:
            window.destroy_output_panel(HELIUM_OBJECT_INSPECT_PANEL)
        view = window.create_output_panel(HELIUM_OBJECT_INSPECT_PANEL)
        try:
            self._logger.debug(reply)
            text = remove_ansi_escape(reply["text/plain"])
            view.run_command("append", {"characters": text})
            window.run_command(
                "show_panel", {"panel": "output." + HELIUM_OBJECT_INSPECT_PANEL}
            )

        except KeyError as ex:
            self._logger.exception(ex)

    def get_view(self):  # type: (...) -> sublime.View
        """Get view corresponds to the KernelConnection."""
        view = None
        view_name = self.view_name
        window = sublime.active_window()
        views = window.views()
        for view_candidate in views:
            if view_candidate.name() == view_name:
                return view_candidate
        if not view:
            active_group = window.active_group()
            view = window.new_file()
            view.set_name(view_name)
            num_group = window.num_groups()
            if num_group != 1:
                if active_group + 1 < num_group:
                    new_group = active_group + 1
                else:
                    new_group = active_group - 1
                window.set_view_index(
                    view, new_group, len(window.sheets_in_group(new_group))
                )
            return view

    def execute_code(
        self, code: str, phantom_region: sublime.Region, view: sublime.View
    ):  # type: (...) -> None
        """Run code with Jupyter kernel."""
        msg_id = self.client.execute(code)
        self.id2region[msg_id] = (
            view,
            sublime.Region(phantom_region.end(), phantom_region.end()),
        )
        info_message = "Kernel executed code ```{code}```.".format(code=code)
        self._logger.info(info_message)

    def is_alive(self):
        """Return True if kernel is alive."""
        return self.client.hb_channel.is_beating()

    def get_complete(
        self, code: str, cursor_pos: int, timeout: Optional[int] = None
    ):  # type: (...) -> List[Tuple[str, str]]
        """Generate complete request."""
        if self.execution_state is not ExecState.IDLE:
            return []
        msg_id = self.client.complete(code, cursor_pos)
        self.shell_msg_queues_lock.acquire()
        try:
            queue = self.shell_msg_queues[msg_id]
        finally:
            self.shell_msg_queues_lock.release()

        try:
            recv_msg = queue.get(timeout=timeout)
            recv_content = recv_msg["content"]
            self._logger.info(recv_content)
            if "_jupyter_types_experimental" in recv_content.get("metadata", {}):
                # If the reply has typing metadata, use it.
                # This metadata for typing is obviously experimental
                # and not documented yet.
                return [
                    (
                        match["text"]
                        + "\t"
                        + (
                            "<no type info>" if match["type"] is None else match["type"]
                        ),
                        match["text"],
                    )
                    for match in recv_content["metadata"]["_jupyter_types_experimental"]
                ]
            else:
                # Just say the completion is came from this plugin, otherwise.
                return [
                    (match + "\tHelium", match) for match in recv_content["matches"]
                ]
        except Empty:
            self._logger.info("Completion timeout.")
        except Exception as ex:
            self._logger.exception(ex)
        finally:
            self.shell_msg_queues_lock.acquire()
            try:
                self.shell_msg_queues.pop(msg_id, None)
            finally:
                self.shell_msg_queues_lock.release()

        return []

    def get_inspection(
        self,
        code: str,
        cursor_pos: int,
        detail_level: int = 0,
        timeout: Optional[int] = None
    ):
        """Get object inspection by sending a `inspect_request` message to kernel."""
        msg_id = self.client.inspect(code, cursor_pos, detail_level)
        self.shell_msg_queues_lock.acquire()
        try:
            queue = self.shell_msg_queues[msg_id]
        finally:
            self.shell_msg_queues_lock.release()

        try:
            recv_msg = queue.get(timeout=timeout)
            self._handle_inspect_reply(recv_msg["content"]["data"])
        except Empty:
            self._logger.info("Object inspection timeout.")

        finally:
            self.shell_msg_queues_lock.acquire()
            try:
                self.shell_msg_queues.pop(msg_id, None)
            finally:
                self.shell_msg_queues_lock.release()
