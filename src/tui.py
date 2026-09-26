from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Header, Footer, ProgressBar, TabbedContent, TabPane, RichLog, Static, Button, Markdown, ListView, ListItem
from textual.binding import Binding
from textual.screen import ModalScreen
from textual import work
import sys
import os
import threading

class ConfirmModal(ModalScreen):
    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("CONFIRM COMMIT\n\nTask verified. Ready to commit?", id="message")
            with Horizontal():
                yield Button("Commit", id="commit", variant="success")
                yield Button("Rollback", id="rollback", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "commit":
            self.dismiss(True)
        else:
            self.dismiss(False)

class MissionControl(App):
    CSS = """
    #left_pane { width: 30%; height: 100%; border-right: solid green; }
    #right_pane { width: 70%; height: 100%; }
    #dialog {
        padding: 1 2;
        border: thick $background 80%;
        background: $surface;
        width: 60;
        height: 15;
        align: center middle;
    }
    """
    
    BINDINGS = [
        Binding("r", "rollback", "Manual Rollback"),
        Binding("q", "quit", "Quit")
    ]
    
    def __init__(self, orchestrator, task: str, **kwargs):
        super().__init__(**kwargs)
        self.orchestrator = orchestrator
        self.harness_task = task
        self.confirm_event = threading.Event()
        self.confirm_result = False
        
        self.log_queue = []
        self.original_stdout = sys.stdout
        sys.stdout = self
        
    def write(self, s):
        self.log_queue.append(s)
        self.original_stdout.write(s)
        
    def flush(self):
        self.original_stdout.flush()

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="left_pane"):
                yield Static("▸ Plan")
                yield ListView(
                    ListItem(Static(" 1. Ingest Task")),
                    ListItem(Static(" 2. Navigate Repo")),
                    ListItem(Static(" 3. Write Patch")),
                    ListItem(Static(" 4. Run Tests")),
                    ListItem(Static(" 5. Confirm Gate"))
                )
                yield Static("\nIteration Cap:")
                yield ProgressBar(total=8, show_eta=False, id="progress")
            with Vertical(id="right_pane"):
                with TabbedContent(initial="log_tab"):
                    with TabPane("Live Log", id="log_tab"):
                        yield RichLog(id="live_log", highlight=True, markup=True)
                    with TabPane("Diff", id="diff_tab"):
                        yield Markdown("No diff yet", id="diff_view")
                    with TabPane("Test Output", id="test_tab"):
                        yield RichLog(id="test_log")
                    with TabPane("Artifacts", id="artifacts_tab"):
                        with Horizontal():
                            yield Button("TASK.md", id="btn_task")
                            yield Button("status.json", id="btn_status")
                            yield Button("OUTCOME.md", id="btn_outcome")
                        yield Markdown("", id="artifact_view")
        yield Footer()
        
    def on_mount(self):
        self.set_interval(0.1, self.update_logs)
        self.set_interval(0.5, self.update_status)
        self.run_orchestrator()

    def update_logs(self):
        if self.log_queue:
            live_log = self.query_one("#live_log", RichLog)
            for s in self.log_queue:
                live_log.write(s.strip("\n"))
            self.log_queue.clear()
            
    def update_status(self):
        pb = self.query_one("#progress", ProgressBar)
        pb.progress = self.orchestrator.status.get("iteration", 0)
        
        patch_path = "/tmp/patch.diff"
        if os.path.exists(patch_path):
            with open(patch_path, "r") as f:
                diff_view = self.query_one("#diff_view", Markdown)
                diff_view.update(f"```diff\n{f.read()}\n```")

    @work(thread=True)
    def run_orchestrator(self):
        self.orchestrator.confirm_callback = self.ask_confirmation
        self.orchestrator.run_task(self.harness_task)
        
    def ask_confirmation(self):
        auto_approve = os.environ.get("AUTO_APPROVE") == "1"
        if auto_approve:
            return True
            
        self.confirm_event.clear()
        self.call_from_thread(self.show_confirm_modal)
        self.confirm_event.wait()
        return self.confirm_result
        
    def show_confirm_modal(self):
        def check_result(result):
            self.confirm_result = result
            self.confirm_event.set()
        self.push_screen(ConfirmModal(), check_result)

    def action_rollback(self):
        self.orchestrator.rollback()
        self.query_one("#live_log", RichLog).write("[bold red]Manual Rollback Triggered via TUI[/bold red]")
        
    def on_button_pressed(self, event: Button.Pressed):
        artifact_view = self.query_one("#artifact_view", Markdown)
        file_map = {
            "btn_task": "TASK.md",
            "btn_status": "status.json",
            "btn_outcome": "OUTCOME.md"
        }
        if event.button.id in file_map:
            path = os.path.join(self.orchestrator.repo_root, file_map[event.button.id])
            if os.path.exists(path):
                with open(path, "r") as f:
                    content = f.read()
                    if path.endswith(".json"):
                        content = f"```json\n{content}\n```"
                    artifact_view.update(content)
            else:
                artifact_view.update(f"{file_map[event.button.id]} not found.")
