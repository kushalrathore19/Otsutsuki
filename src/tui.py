from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Header, Footer, ProgressBar, TabbedContent, TabPane, RichLog, Static, Button, Markdown, ListView, ListItem, Input
from textual.binding import Binding
from textual.screen import ModalScreen
from textual import work
from rich.syntax import Syntax
import sys
import os
import threading

class ConfirmModal(ModalScreen):
    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("CONFIRM COMMIT\n\nTask verified. Ready to commit?", id="message")
            yield Static(id="modal_diff_view", classes="diff_container")
            with Horizontal():
                yield Button("Commit", id="commit", variant="success")
                yield Button("Rollback", id="rollback", variant="error")

    def on_mount(self):
        patch_path = "/tmp/patch.diff"
        if os.path.exists(patch_path):
            with open(patch_path, "r") as f:
                self.query_one("#modal_diff_view", Static).update(Syntax(f.read(), "diff", theme="monokai", word_wrap=True))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "commit":
            self.dismiss(True)
        elif event.button.id == "rollback":
            self.dismiss(False)

class InterceptModal(ModalScreen):
    def __init__(self, critique, **kwargs):
        super().__init__(**kwargs)
        self.critique = critique

    def compose(self) -> ComposeResult:
        with Vertical(id="intercept_dialog"):
            yield Static("🚨 Pre-Flight Interception 🚨", classes="modal_title")
            matrix_text = (
                f"A better approach was detected!\n\n"
                f"Original Est. Tokens: {self.critique.get('Original_Est_Tokens')}\n"
                f"New Est. Tokens: {self.critique.get('New_Est_Tokens')}\n"
                f"Complexity: {self.critique.get('Complexity')}\n"
                f"Reason: {self.critique.get('Recommendation_Reason')}\n\n"
                f"Suggested Task:\n{self.critique.get('Recommended_Task')}"
            )
            yield Static(matrix_text, id="intercept_message")
            with Horizontal():
                yield Button("Accept Recommendation", id="accept_rec", variant="success")
                yield Button("Override & Execute Original", id="override_rec", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "accept_rec":
            self.dismiss(self.critique.get('Recommended_Task'))
        elif event.button.id == "override_rec":
            self.dismiss(None)

class NudgeModal(ModalScreen):
    def compose(self) -> ComposeResult:
        with Vertical(id="nudge_dialog"):
            yield Static("Course-Correct (Nudge Agent)", classes="modal_title")
            yield Input(placeholder="Enter hint for the agent...", id="nudge_input")
            with Horizontal():
                yield Button("Send", id="send_nudge", variant="success")
                yield Button("Cancel", id="cancel_nudge", variant="error")
                
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "send_nudge":
            hint = self.query_one("#nudge_input", Input).value
            self.dismiss(hint)
        else:
            self.dismiss(None)

class MissionControl(App):
    CSS = """
    #left_pane { width: 30%; height: 100%; border-right: solid green; }
    #right_pane { width: 70%; height: 100%; }
    #dialog, #nudge_dialog, #intercept_dialog {
        padding: 1 2;
        border: thick $background 80%;
        background: $surface;
        width: 80%;
        height: 80%;
        align: center middle;
    }
    #nudge_dialog, #intercept_dialog { height: auto; }
    .diff_container { height: 1fr; overflow-y: auto; margin-bottom: 1; }
    #odometer { padding-left: 2; padding-right: 2; background: $boost; color: $text; }
    """
    
    BINDINGS = [
        Binding("r", "rollback", "Manual Rollback"),
        Binding("ctrl+n", "nudge", "Nudge Agent"),
        Binding("q", "quit", "Quit")
    ]
    
    def __init__(self, orchestrator, task: str, **kwargs):
        super().__init__(**kwargs)
        self.orchestrator = orchestrator
        self.harness_task = task
        self.confirm_event = threading.Event()
        self.confirm_result = False
        
        self.intercept_event = threading.Event()
        self.intercept_result = None
        
        self.log_queue = []
        from logging_setup import setup_logging
        setup_logging(self.log_queue)

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
                        yield Static("No diff yet", id="diff_view", classes="diff_container")
                    with TabPane("Test Output", id="test_tab"):
                        yield RichLog(id="test_log")
                    with TabPane("Artifacts", id="artifacts_tab"):
                        with Horizontal():
                            yield Button("TASK.md", id="btn_task")
                            yield Button("status.json", id="btn_status")
                            yield Button("OUTCOME.md", id="btn_outcome")
                        yield Markdown("", id="artifact_view")
        yield Static("Tokens: 0 | Cost: $0.000000", id="odometer")
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
        
        # Token odometer
        tokens = self.orchestrator.status.get("tokens", 0)
        cost = (tokens / 1_000_000) * 0.20
        self.query_one("#odometer", Static).update(f"Tokens: {tokens} | Cost: ${cost:.6f}")
        
        # Diff update
        patch_path = "/tmp/patch.diff"
        if os.path.exists(patch_path):
            with open(patch_path, "r") as f:
                content = f.read()
                diff_view = self.query_one("#diff_view", Static)
                diff_view.update(Syntax(content, "diff", theme="monokai", word_wrap=True))

    @work(thread=True)
    def run_orchestrator(self):
        self.orchestrator.confirm_callback = self.ask_confirmation
        self.orchestrator.intercept_callback = self.ask_intercept
        self.orchestrator.run_task(self.harness_task)
        
    def ask_intercept(self, critique, original_task):
        auto_approve = os.environ.get("AUTO_APPROVE") == "1"
        if auto_approve:
            return original_task
            
        self.intercept_event.clear()
        self.call_from_thread(self.show_intercept_modal, critique)
        self.intercept_event.wait()
        return self.intercept_result or original_task
        
    def show_intercept_modal(self, critique):
        def check_result(result):
            self.intercept_result = result
            self.intercept_event.set()
        self.push_screen(InterceptModal(critique), check_result)
        
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
        
    def action_nudge(self):
        def check_nudge(hint):
            if hint:
                self.orchestrator.nudge_queue.append(hint)
                self.query_one("#live_log", RichLog).write(f"[bold cyan]Nudge injected: {hint}[/bold cyan]")
        self.push_screen(NudgeModal(), check_nudge)
        
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
