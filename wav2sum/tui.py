import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import DataTable, Markdown, Static

from wav2sum.client import DaemonClient, DaemonError
from wav2sum.config import HISTORY_PATH, LOG_PATH, Config

APP_DEFAULTS = "ai.chudinov.wav2sum"
TRANSCRIPT_LINE = re.compile(r"^\[(\S+) – \S+\] ([^:]+): (.*)$")


class StatusPanel(Static, can_focus=True):
    def on_focus(self) -> None:
        self.app.show_log()


class ListPanel(DataTable):
    BINDINGS: ClassVar = [Binding("j", "cursor_down", show=False), Binding("k", "cursor_up", show=False)]

    def __init__(self, id: str, title: str, *columns: str):
        super().__init__(id=id, classes="panel", cursor_type="row", show_header=False, zebra_stripes=False)
        self.border_title = title
        self.columns_spec = columns

    def on_mount(self) -> None:
        self.add_columns(*self.columns_spec)

    def on_focus(self) -> None:
        if self.row_count:
            self.app.show_row(self, self.coordinate_to_cell_key(self.cursor_coordinate).row_key.value)


class Wav2SumTUI(App):
    TITLE = "wav2sum"
    CSS = """
    Screen { layout: horizontal; }
    #left { width: 44%; }
    .panel {
        border: round $panel-lighten-2;
        border-title-color: $text-muted;
        background: $background;
        scrollbar-size-vertical: 1;
        overflow-x: hidden;
    }
    .panel:focus, .panel:focus-within {
        border: round $success;
        border-title-color: $success;
        border-title-style: bold;
    }
    #status { height: 7; padding: 0 1; }
    #calls, #dictations { height: 1fr; }
    #jobs { height: 7; }
    #detail { width: 1fr; padding: 0 1; }
    DataTable > .datatable--cursor { background: $primary 45%; }
    DataTable:blur > .datatable--cursor { background: $panel-lighten-1; }
    #hints { dock: bottom; height: 1; padding: 0 1; color: $text-muted; }
    """
    BINDINGS: ClassVar = [
        Binding("1", "focus('status')", show=False),
        Binding("2", "focus('calls')", show=False),
        Binding("3", "focus('dictations')", show=False),
        Binding("4", "focus('jobs')", show=False),
        Binding("0", "focus('detail')", show=False),
        Binding("r", "record", show=False),
        Binding("d", "daemon", show=False),
        Binding("o", "open", show=False),
        Binding("q", "quit", show=False),
    ]

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self.client: DaemonClient | None = None
        self.status: dict = {}
        self.levels = (-120.0, -120.0)
        self.dictations: list[dict] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="left"):
            status = StatusPanel(id="status", classes="panel")
            status.border_title = "[1] Status"
            yield status
            yield ListPanel("calls", "[2] Calls", "Recording", "Length", "Speakers")
            yield ListPanel("dictations", "[3] Dictations", "When", "Text")
            yield ListPanel("jobs", "[4] Processing", "Recording", "State")
        with VerticalScroll(id="detail", classes="panel"):
            yield Markdown(id="view")
            yield Static(id="log", markup=False)
        yield Static(id="hints")

    def on_mount(self) -> None:
        self.query_one("#detail").border_title = "[0] Details"
        self.load_calls()
        self.load_dictations()
        self.set_interval(0.5, self.refresh_status)
        self.follow_daemon()
        self.query_one("#calls").focus()

    def load_calls(self) -> None:
        table = self.query_one("#calls", DataTable)
        table.clear()
        folders = [d for d in self.cfg.output_dir.glob("*") if (d / "meta.json").exists()]
        for folder in sorted(folders, key=lambda d: d.stat().st_mtime, reverse=True):
            meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
            speakers = ", ".join(meta.get("speakers", []))
            table.add_row(
                folder.name.removeprefix("call-"), _clock(meta.get("duration_sec", 0)), speakers, key=str(folder)
            )

    def load_dictations(self) -> None:
        if HISTORY_PATH.exists():
            lines = HISTORY_PATH.read_text(encoding="utf-8").splitlines()[-500:]
            self.dictations = [json.loads(line) for line in reversed(lines)]
        self.render_dictations()

    def render_dictations(self) -> None:
        table = self.query_one("#dictations", DataTable)
        table.clear()
        for i, entry in enumerate(self.dictations):
            table.add_row(entry["time"][5:16].replace("T", " "), entry["text"].replace("\n", " ")[:80], key=str(i))

    def render_jobs(self) -> None:
        table = self.query_one("#jobs", DataTable)
        table.clear()
        for job in reversed(self.status.get("jobs", [])):
            state = {"queued": "queued", "done": "done", "failed": "failed"}.get(job["state"], job["stage"] or "…")
            table.add_row(Path(job["audio"]).stem.removeprefix("call-"), state, key=str(job["id"]))

    @on(DataTable.RowHighlighted)
    def row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key.value is not None and event.data_table.has_focus:
            self.show_row(event.data_table, event.row_key.value)

    def show_row(self, table: DataTable, key: str) -> None:
        match table.id:
            case "calls":
                self.show("Call", _call_markdown(Path(key)))
            case "dictations":
                self.show("Dictation", _dictation_markdown(self.dictations[int(key)]))
            case "jobs":
                job = next((j for j in self.status.get("jobs", []) if str(j["id"]) == key), None)
                if job:
                    self.show("Processing", _job_markdown(job))

    def show_log(self) -> None:
        lines = LOG_PATH.read_text(encoding="utf-8").splitlines()[-60:] if LOG_PATH.exists() else ["no log yet"]
        self.show("Daemon log", Text("\n".join(lines), style="dim", overflow="fold"))
        self.call_after_refresh(self.query_one("#detail").scroll_end, animate=False)

    def show(self, title: str, content: str | Text) -> None:
        detail = self.query_one("#detail")
        detail.border_title = f"[0] {title}"
        view, log = self.query_one("#view", Markdown), self.query_one("#log", Static)
        view.display, log.display = not isinstance(content, Text), isinstance(content, Text)
        if isinstance(content, Text):
            log.update(content)
        else:
            view.update(content)
            detail.scroll_home(animate=False)

    @work(exclusive=True)
    async def follow_daemon(self) -> None:
        while True:
            try:
                self.client = await DaemonClient.connect()
                self.status = await self.client.request("status")
                self.render_jobs()
                async for event in self.client.events():
                    self.handle(event)
            except DaemonError:
                pass
            self.client = None
            self.status = {}
            await asyncio.sleep(2)

    def handle(self, event: dict) -> None:
        payload = {k: v for k, v in event.items() if k != "event"}
        match event["event"]:
            case "state":
                self.status["state"] = event["state"]
            case "level":
                self.levels = (event["mic_db"], event["sys_db"])
                self.status["recording"] = {**(self.status.get("recording") or {}), "seconds": event["seconds"]}
            case "recording":
                self.status["recording"] = {"path": event["path"], "seconds": 0} if event["active"] else None
                if event["active"] and event.get("auto"):
                    self.notify(f"Recording {event['auto']} call; stops by itself when the call ends")
            case "job":
                self.status["jobs"] = [j for j in self.status.get("jobs", []) if j["id"] != event["id"]] + [payload]
                self.render_jobs()
                if event["state"] == "done":
                    self.load_calls()
                    self.notify(f"Summary ready: {Path(event['out_dir']).name}")
                elif event["state"] == "failed":
                    self.notify(f"Processing failed: {event['error']}", severity="error")
            case "dictation":
                self.dictations.insert(0, payload)
                self.render_dictations()

    def refresh_status(self) -> None:
        if not self.client:
            lines = ["[dim]○ background process is off[/]", "", "[b]d[/] to start it"]
        elif self.status.get("state") != "ready":
            lines = ["[yellow]◌ loading models…[/]"]
        else:
            memory = self.status.get("memory_mb", 0) + self.status.get("llm_memory_mb", 0)
            lines = [
                "[green]● ready[/]",
                f"memory  {memory / 1024:.1f} GB  [dim](models {self.status.get('memory_mb', 0)} MB · "
                f"LLM {self.status.get('llm_memory_mb', 0)} MB)[/]",
                f"dictation  hold {_hotkey_label(self.status.get('hotkey', ''))}",
            ]
        if recording := self.status.get("recording"):
            mic, system = self.levels
            lines.append(f"[red]● rec {_clock(recording.get('seconds', 0))}[/]  me {_bar(mic)}  them {_bar(system)}")
        self.query_one("#status", Static).update("\n".join(lines))

        record = "stop recording" if self.status.get("recording") else "record"
        daemon = "stop daemon" if self.client else "start daemon"
        hints = [
            ("r", record),
            ("d", daemon),
            ("o", "show in Finder"),
            ("1-4", "panels"),
            ("j/k", "up/down"),
            ("q", "quit"),
        ]
        self.query_one("#hints", Static).update("  ".join(f"[b $accent]{k}[/] {v}" for k, v in hints))

    async def action_record(self) -> None:
        if not self.client or self.status.get("state") != "ready":
            self.notify("The background process is not ready", severity="warning")
            return
        try:
            await self.client.request("record_stop" if self.status.get("recording") else "record_start")
        except DaemonError as e:
            self.notify(str(e), severity="error")

    async def action_daemon(self) -> None:
        wanted = self.client is None
        subprocess.run(
            ["defaults", "write", APP_DEFAULTS, "daemonWanted", "-bool", "YES" if wanted else "NO"], check=False
        )
        if wanted:
            subprocess.Popen(
                [sys.executable, "-m", "wav2sum", "serve"],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.notify("Starting the background process…")
        else:
            await self.client.request("shutdown")

    def action_focus(self, panel: str) -> None:
        self.query_one(f"#{panel}").focus()

    def action_open(self) -> None:
        table = self.query_one("#calls", DataTable)
        if table.row_count:
            subprocess.run(["open", table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value], check=False)


def _call_markdown(folder: Path) -> str:
    summary = folder / "summary.md"
    parts = [
        f"# {folder.name}",
        summary.read_text(encoding="utf-8") if summary.exists() else "_No summary_",
        "---",
        "## Transcript",
    ]
    transcript = folder / "transcript.txt"
    if transcript.exists():
        for line in transcript.read_text(encoding="utf-8").split("\n\n"):
            match = TRANSCRIPT_LINE.match(line)
            parts.append(f"`{match[1]}` **{match[2]}:** {match[3]}" if match else line)
    return "\n\n".join(parts)


def _dictation_markdown(entry: dict) -> str:
    timings = entry.get("timings", {})
    mode = "command" if entry.get("command") else entry.get("style", "")
    return (
        f"**{entry['time'][:19].replace('T', ' ')}** · `{entry.get('app') or '—'}` · {mode}\n\n"
        f"{entry['text']}\n\n---\n\n**Recognized:** {entry['raw']}\n\n"
        f"_{entry.get('seconds', 0)} s of audio · ASR {timings.get('asr', 0)} s · LLM {timings.get('llm', 0)} s_"
    )


def _job_markdown(job: dict) -> str:
    lines = [f"# {Path(job['audio']).name}", f"**State:** {job['state']} {job['stage']}", f"`{job['audio']}`"]
    if job.get("out_dir"):
        lines.append(f"**Output:** `{job['out_dir']}`")
    if job.get("error"):
        lines.append(f"**Error:** {job['error']}")
    return "\n\n".join(lines)


def _hotkey_label(key: str) -> str:
    return {"right_option": "right ⌥", "right_command": "right ⌘", "fn": "fn"}.get(key, key)


def _clock(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m // 60}:{m % 60:02d}:{s:02d}" if m >= 60 else f"{m:02d}:{s:02d}"


def _bar(db: float, width: int = 8) -> str:
    filled = max(0, min(width, round((db + 60) / 60 * width)))
    return "▮" * filled + "▯" * (width - filled)
