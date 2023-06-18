"""Definition of KernelConnection class.

KernelConnection class provides interaction with Jupyter kernels.

Copyright (c) 2017-2018, NEGORO Tetsuya (https://github.com/ngr-t)
"""
import re
from collections import defaultdict
from datetime import datetime
from queue import Empty, Queue
from threading import Event, RLock, Thread

import sublime

from .utils import get_cell, get_png_dimensions, show_password_input

JUPYTER_PROTOCOL_VERSION = "5.0"

REPLY_STATUS_OK = "ok"
REPLY_STATUS_ERROR = "error"
REPLY_STATUS_ABORT = "abort"

MSG_TYPE_EXECUTE_INPUT = "execute_input"
MSG_TYPE_EXECUTE_REQUEST = "execute_request"
MSG_TYPE_EXECUTE_RESULT = "execute_result"
MSG_TYPE_EXECUTE_REPLY = "execute_reply"
MSG_TYPE_COMPLETE_REQUEST = "complete_request"
MSG_TYPE_COMPLETE_REPLY = "complete_reply"
MSG_TYPE_DISPLAY_DATA = "display_data"
MSG_TYPE_INSPECT_REQUEST = "inspect_request"
MSG_TYPE_INSPECT_REPLY = "inspect_reply"
MSG_TYPE_INPUT_REQUEST = "input_request"
MSG_TYPE_INPUT_REPLY = "input_reply"
MSG_TYPE_ERROR = "error"
MSG_TYPE_STREAM = "stream"
MSG_TYPE_STATUS = "status"

HELIUM_FIGURE_PHANTOMS = "helium_figure_phantoms"
MAX_PHANTOMS = 65536

# Used as key of status bar.
KERNEL_STATUS_KEY = "helium_kernel_status"

HELIUM_OBJECT_INSPECT_PANEL = "helium_object_inspect"

ANSI_ESCAPE_PATTERN = re.compile(r"\x1b[^m]*m")
DOLLAR_REGEX = re.compile(r"(?<!\\)\$")

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

IMAGE_PHANTOM = """<body id="helium-image-result" style="background-color:none">
  <style>
    .image {{ background-color: none }}
    .closebutton {{ text-decoration: none }}
  </style>
  <a class=closebutton href=hide>×</a>
  <br>
  <img class="image" alt="Out" style="width: {width}; height: {height}" src="data:image/png;base64,{data}" />
</body>"""

STREAM_PHANTOM = "<div class={name}>{content}</div>"


def fix_whitespace_for_phantom(text: str):
    """Transform output for proper display.

    This is important to display pandas DataFrames, for instance.
    """
    text = text.replace(" ", r"&nbsp;")
    text = "<br>".join(text.splitlines())
    return text


def extract_content(messages, msg_type):
    """Extract content from messages received from a kernel."""
    return [
        message["content"]
        for message in messages
        if message["header"]["msg_type"] == msg_type
    ]


def remove_ansi_escape(text: str):
    return ANSI_ESCAPE_PATTERN.sub("", text)


# Dollar sign have meanings in ST completions but not in Jupyter
# We must therefore escape them
def escape_dollar_signs(string):
    """Escape any dollar sign present in string"""
    return DOLLAR_REGEX.sub(r'\\$', string)



def get_msg_type(message):
    return message["header"]["msg_type"]


def extract_data(result):
    """Extract plain text data."""
    try:
        return result["data"]
    except KeyError:
        return ""


class KernelConnection(object):
    """Interact with a Jupyter kernel."""

    class MessageReceiver(Thread):  # noqa
        def __init__(self, kernel):
            """Initialize AsyncCommunicator class."""
            super().__init__()
            self._kernel = kernel
            self.exit = Event()

        def shutdown(self):
            self.exit.set()

    class ShellMessageReceiver(MessageReceiver):
        """Communicator that runs asynchroniously."""

        def run(self):
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
                except Empty:
                    pass
                except Exception as ex:
                    self._kernel._logger.exception(ex)

    class IOPubMessageReceiver(MessageReceiver):
        """Receive and process IOPub messages."""

        def run(self):
            """Run main routine."""
            # TODO: log, handle other message types.

            while not self.exit.is_set():
                try:
                    msg = self._kernel.client.get_iopub_msg(timeout=1)
                    self._kernel._logger.info(msg)
                    content = msg.get("content", {})
                    execution_count = content.get("execution_count", None)
                    msg_type = msg["msg_type"]
                    view, region = self._kernel.id2region.get(
                        msg["parent_header"].get("msg_id", None), (None, None)
                    )

                    if msg_type == MSG_TYPE_STATUS:
                        self._kernel._execution_state = content["execution_state"]
                    elif msg_type == MSG_TYPE_EXECUTE_INPUT:
                        # if code is executed deleted all phantoms in this region
                        self._kernel._clear_phantoms_in_region(region, view)

                        self._kernel._write_text_to_view("\n\n")
                        if sublime.load_settings("Helium.sublime-settings").get(
                            "output_code"
                        ):
                            self._kernel._output_input_code(
                                content["code"], content["execution_count"]
                            )
                    elif msg_type == MSG_TYPE_ERROR:
                        self._kernel._logger.info("Handling error")
                        self._kernel._handle_error(
                            content["ename"],
                            content["evalue"],
                            content["traceback"],
                            region,
                            view,
                        )
                    elif msg_type == MSG_TYPE_DISPLAY_DATA:
                        self._kernel._write_mime_data_to_view(
                            content["data"], region, view
                        )
                    elif msg_type == MSG_TYPE_EXECUTE_RESULT:
                        self._kernel._write_mime_data_to_view(
                            content["data"], region, view
                        )
                    elif msg_type == MSG_TYPE_STREAM:
                        self._kernel._handle_stream(
                            content["name"],
                            content["text"],
                            region,
                            view,
                        )

                except Empty:
                    pass
                except Exception as ex:
                    self._kernel._logger.exception(ex)

    class StdInMessageReceiver(MessageReceiver):
        """Receive and process IOPub messages."""

        def _handle_input_request(self, prompt, password):
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

        def run(self):
            """Run main routine."""
            # TODO: log, handle other message types.
            while not self.exit.is_set():
                try:
                    msg = self._kernel.client.get_stdin_msg(timeout=1)
                    msg_type = msg["msg_type"]
                    content = msg["content"]
                    if msg_type == MSG_TYPE_INPUT_REQUEST:
                        self._handle_input_request(
                            content["prompt"], content["password"]
                        )
                except Empty:
                    pass
                except Exception as ex:
                    self._kernel._logger.exception(ex)

    def _init_receivers(self):
        # Set the attributes refered by receivers before they start.
        self._shell_msg_receiver = self.ShellMessageReceiver(self)
        self._shell_msg_receiver.start()
        self._iopub_msg_receiver = self.IOPubMessageReceiver(self)
        self._iopub_msg_receiver.start()
        self._stdin_msg_receiver = self.StdInMessageReceiver(self)
        self._stdin_msg_receiver.start()

    def __init__(
        self,
        kernel_id,
        kernel_manager,
        parent,
        connection_name=None,
        logger=None,
    ):
        """Initialize KernelConnection class.

        parameters
        ---------
        kernel_id str: kernel ID
        parent parent kernel manager
        """
        self._logger = logger
        self.shell_msg_queues = defaultdict(Queue)
        self._kernel_id = kernel_id
        self.parent = parent
        self.kernel_manager = kernel_manager
        self.client = self.kernel_manager.client()
        self.client.start_channels()
        self.shell_msg_queues_lock = RLock()
        self.id2region = {}
        self._connection_name = connection_name
        self._execution_state = "unknown"
        self._init_receivers()
        self.phantoms = {}

    def __del__(self):  # noqa
        self._shell_msg_receiver.shutdown()
        self._iopub_msg_receiver.shutdown()
        self._stdin_msg_receiver.shutdown()

    @property
    def lang(self):
        """Language of kernel."""
        return self.kernel_manager.kernel_name

    @property
    def kernel_id(self):
        """ID of kernel."""
        return self._kernel_id

    def shutdown_kernel(self):
        self.kernel_manager.shutdown_kernel()

    def restart_kernel(self):
        self.kernel_manager.restart_kernel()

    def interrupt_kernel(self):
        self.kernel_manager.interrupt_kernel()

    def get_connection_name(self):
        return self._connection_name

    def set_connection_name(self, new_name):
        # We also have to change the view name now.
        view = self.get_view()
        self._connection_name = new_name
        view.set_name(self.view_name)

    def del_connection_name(self):
        self._connection_name = None

    connection_name = property(
        get_connection_name,
        set_connection_name,
        del_connection_name,
        "Name of kernel connection shown in a view title.",
    )

    @property
    def view_name(self):
        """Return name of output view."""
        return "*Helium Output* {repr}".format(repr=self.repr)

    @property
    def repr(self):
        """Return string representation of the connection."""
        if self.connection_name:
            return "{connection_name} ([{lang}] {kernel_id})".format(
                connection_name=self.connection_name,
                lang=self.lang,
                kernel_id=self.kernel_id,
            )
        else:
            return "[{lang}] {kernel_id}".format(
                lang=self.lang, kernel_id=self.kernel_id
            )

    @property
    def execution_state(self):
        return self._execution_state

    @property
    def _show_inline_output(self):
        return sublime.load_settings("Helium.sublime-settings").get("inline_output")

    def activate_view(self):
        """Activate view to show the output of kernel."""
        view = self.get_view()
        current_view = sublime.active_window().active_view()
        sublime.active_window().focus_view(view)
        view.set_scratch(True)  # avoids prompting to save
        view.settings().set("word_wrap", "false")
        sublime.active_window().focus_view(current_view)

    def _output_input_code(self, code, execution_count):
        line = "In[{execution_count}]: {code}".format(
            execution_count=execution_count, code=code
        )
        self._write_text_to_view(line)

    def _handle_error(
        self,
        ename,
        evalue,
        traceback,
        region: sublime.Region = None,
        view: sublime.View = None,
    ) -> None:
        try:
            lines = """\nError: {ename}, {evalue}.
            \nTraceback:\n{traceback}""".format(
                ename=ename,
                evalue=evalue,
                traceback="\n".join(traceback),
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
        self, name, text, region: sublime.Region = None, view: sublime.View = None
    ) -> None:
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

    def _write_out_execution_count(self, execution_count) -> None:
        self._write_text_to_view("\nOut[{}]: \n".format(execution_count))

    def _write_text_to_view(self, text: str) -> None:
        if self._show_inline_output:
            return
        self.activate_view()
        view = self.get_view()
        view.set_read_only(False)
        view.run_command("append", {"characters": text})
        view.set_read_only(True)
        view.show(view.size())

    def _write_phantom(self, content: str):
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
    ):
        if self._show_inline_output:
            id = HELIUM_FIGURE_PHANTOMS + datetime.now().isoformat()

            html = TEXT_PHANTOM.format(content=content)
            self._add_phantom(view, id, region, html)

    def _write_inline_image_phantom(
        self, data: str, region: sublime.Region, view: sublime.View
    ):
        if self._show_inline_output:
            id = HELIUM_FIGURE_PHANTOMS + datetime.now().isoformat()
            img_size = sublime.load_settings("Helium.sublime-settings").get(
                "image_size", "optimal"
            )

            width = view.viewport_extent()[0] - 2
            dimensions = get_png_dimensions(data)

            if img_size == "original" or (
                img_size == "optimal" and (dimensions[0] < width)
            ):
                html = IMAGE_PHANTOM.format(
                    data=data, width=dimensions[0], height=dimensions[1]
                )
            else:
                scale_factor = width / dimensions[0]
                height = dimensions[1] * scale_factor

                html = IMAGE_PHANTOM.format(data=data, width=width, height=height)

            self._add_phantom(view, id, region, html)

    def _add_phantom(
        self, view: sublime.View, id: str, region: sublime.Region, html: str
    ):
        int_id = view.add_phantom(
            id,
            region,
            html,
            sublime.LAYOUT_BLOCK,
            on_navigate=lambda href, id=id, view=view: self._erase_phantom(
                id, view=view
            ),
        )
        self.phantoms[id] = int_id

        if int_id > MAX_PHANTOMS:
            sublime.message_dialog(
                "Please close and reopen your current tab. "
                + "Otherwise the 'Clear All Cells' command might not work as intended."
            )
        self._logger.info("Created inline phantom image")

    def _clear_phantoms_in_region(self, region: sublime.Region, view: sublime.View):
        _, cell = get_cell(view, region, logger="")
        remove = [
            pid
            for pid, int_id in self.phantoms.items()
            if cell.contains(view.query_phantom(int_id)[0])
        ]

        for pid in remove:
            self._erase_phantom(pid, view=view)

    def _erase_phantom(self, pid: str, *, view: sublime.View):
        if pid in self.phantoms:
            _ = self.phantoms.pop(pid)
            view.erase_phantoms(pid)

    def _write_mime_data_to_view(
        self, mime_data: dict, region: sublime.Region, view: sublime.View
    ) -> None:
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

            img_size = sublime.load_settings("Helium.sublime-settings").get(
                "image_size", "optimal"
            )

            self._logger.info(self.get_view().viewport_extent())
            width = self.get_view().viewport_extent()[0] - 2
            dimensions = get_png_dimensions(data)

            if img_size == "original" or (
                img_size == "optimal" and (dimensions[0] < width)
            ):
                width, height = dimensions
            else:
                scale_factor = width / dimensions[0]
                height = dimensions[1] * scale_factor

            content = (
                '<body style="background-color: none">'
                '<img alt="Out" style="width: {width}; height: {height}" src="data:image/png;base64,{data}" />'
                + "</body>"
            ).format(data=data, width=width, height=height, bgcolor="white")

            self._write_phantom(content)
            self._write_inline_image_phantom(data, region, view)

    def _handle_inspect_reply(self, reply: dict):
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

    def get_view(self):
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
            view.settings().set("syntax", "Packages/Helium/Helium.sublime-syntax")
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

    def execute_code(self, code, phantom_region, view):
        """Run code with Jupyter kernel."""
        msg_id = self.client.execute(code)
        self.id2region[msg_id] = (
            view,
            sublime.Region(phantom_region.end() - 1, phantom_region.end() - 1),
        )
        info_message = "Kernel executed code ```{code}```.".format(code=code)
        self._logger.info(info_message)

    def is_alive(self):
        """Return True if kernel is alive."""
        return self.client.hb_channel.is_beating()

    def get_complete(self, code, cursor_pos, timeout=None):
        """Generate complete request."""
        if self.execution_state != "idle":
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
                        escape_dollar_signs(match["text"]),
                    )
                    for match in recv_content["metadata"]["_jupyter_types_experimental"]
                ]
            else:
                # Just say the completion is came from this plugin, otherwise.
                return [
                    (match + "\tHelium", escape_dollar_signs(match)) for match in recv_content["matches"]
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

    def get_inspection(self, code, cursor_pos, detail_level=0, timeout=None):
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
