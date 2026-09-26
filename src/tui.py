from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Header, Footer, ProgressBar, TabbedContent, TabPane, RichLog, Static, Button, Markdown, ListView, ListItem, Input, TextArea
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
                
    def on_mount(self) -> None:
        self.query_one("#nudge_input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "send_nudge":
            hint = self.query_one("#nudge_input", Input).value
            self.dismiss(hint)
        else:
            self.dismiss(None)

class TaskEntryModal(ModalScreen):
    def __init__(self, initial_task, **kwargs):
        super().__init__(**kwargs)
        self.initial_task = initial_task
        
    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("NEW TASK\n\nEnter task description or paste GitHub issue URL/text.", classes="modal_title")
            yield TextArea(self.initial_task, id="task_input")
            with Horizontal():
                yield Button("Start", id="start_task", variant="success")
                
    def on_mount(self) -> None:
        self.query_one("#task_input", TextArea).focus()
                
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "start_task":
            task_text = self.query_one("#task_input", TextArea).text
            if task_text.strip():
                self.dismiss(task_text.strip())

class OutcomeModal(ModalScreen):
    def __init__(self, status, **kwargs):
        super().__init__(**kwargs)
        self.status = status
        
    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("TASK FINISHED", classes="modal_title")
            summary = (
                f"Status: {self.status.get('state')}\n"
                f"Iterations: {self.status.get('iteration')}\n"
                f"Tokens: {self.status.get('tokens')}\n"
                f"Rollbacks: {self.status.get('rollback_count')}"
            )
            yield Static(summary)
            with Horizontal():
                yield Button("Continue to New Task", id="continue_task", variant="primary")
                
    def on_mount(self) -> None:
        self.query_one("#continue_task", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "continue_task":
            self.dismiss()

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
    .history_sidebar { width: 30%; height: 100%; border-right: solid green; }
    #history_view { width: 70%; height: 100%; }
    #role_indicator {
        background: blue;
        color: white;
        text-align: center;
        text-style: bold;
        padding: 0 1;
    }
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
        yield Static("Active Role: Single Agent", id="role_indicator")
        with Horizontal():
            with Vertical(id="left_pane"):
                yield Static("▸ Plan")
                yield ListView(
                    ListItem(Static(" 1. Ingest Task")),
                    ListItem(Static(" 2. Navigate Repo")),
                    ListItem(Static(" 3. Write Patch")),
                    ListItem(Static(" 4. Run Tests")),
                    ListItem(Static(" 5. Confirm Gate")),
                    id="plan_list"
                )
                yield Static("\nIteration Cap:")
                yield ProgressBar(total=8, show_eta=False, id="progress")
            with Vertical(id="right_pane"):
                with TabbedContent(initial="log_tab"):
                    with TabPane("Live Log", id="log_tab"):
                        yield RichLog(id="live_log", highlight=True, markup=True, wrap=True)
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
                    with TabPane("History", id="history_tab"):
                        with Horizontal():
                            yield ListView(id="history_list", classes="history_sidebar")
                            yield Markdown("", id="history_view")
        yield Static("Tokens: 0 | Cost: $0.000000", id="odometer")
        yield Footer()
        
    def on_mount(self):
        self.set_interval(0.1, self.update_logs)
        self.set_interval(0.5, self.update_status)
        self.update_history_list()
        
        if os.environ.get("AUTO_APPROVE") == "1":
            self.run_orchestrator()
        else:
            self.prepare_next_task(first_launch=True)
            
    def prepare_next_task(self, first_launch=False):
        if not first_launch:
            self.orchestrator.reset()
            self.query_one("#progress", ProgressBar).progress = 0
            self.query_one("#diff_view", Static).update("No diff yet")
            self.update_history_list()
            
        def check_result(task_text):
            if task_text:
                self.harness_task = task_text
                self.run_orchestrator()
            else:
                self.exit()
                
        self.push_screen(TaskEntryModal(self.harness_task), check_result)

    def update_logs(self):
        if self.log_queue:
            live_log = self.query_one("#live_log", RichLog)
            for s in self.log_queue:
                live_log.write(s.strip("\n"))
            self.log_queue.clear()
            
    def update_status(self):
        pb = self.query_one("#progress", ProgressBar)
        pb.progress = self.orchestrator.status.get("iteration", 0)
        
        tokens = self.orchestrator.status.get("tokens", 0)
        cost = (tokens / 1_000_000) * 0.20
        self.query_one("#odometer", Static).update(f"Tokens: {tokens} | Cost: ${cost:.6f}")
        
        patch_path = "/tmp/patch.diff"
        if os.path.exists(patch_path):
            with open(patch_path, "r") as f:
                content = f.read()
                diff_view = self.query_one("#diff_view", Static)
                diff_view.update(Syntax(content, "diff", theme="monokai", word_wrap=True))
                
        if self.orchestrator.config.multi_agent:
            plan_path = os.path.join(self.orchestrator.repo_root, "PLAN.md")
            if os.path.exists(plan_path):
                try:
                    import json
                    with open(plan_path, "r") as f:
                        plan_data = json.load(f)
                    
                    plan_list = self.query_one("#plan_list", ListView)
                    if len(plan_list.children) != len(plan_data) or any(plan_data[i].get("status") != getattr(plan_list.children[i], "_last_status", None) for i in range(len(plan_data))):
                        plan_list.clear()
                        icon_map = {"pending": "⏳", "running": "🔄", "done": "✅", "failed": "❌", "success": "✅"}
                        for subtask in plan_data:
                            status = subtask.get("status", "pending")
                            icon = icon_map.get(status, "⏳")
                            desc = subtask.get("description", "")
                            item = ListItem(Static(f"{icon} {desc}"))
                            item._last_status = status
                            plan_list.append(item)
                except Exception:
                    pass

    def update_role(self, role):
        self.call_from_thread(self.query_one("#role_indicator", Static).update, f"Active Role: {role}")

    @work(thread=True)
    def run_orchestrator(self):
        self.orchestrator.confirm_callback = self.ask_confirmation
        self.orchestrator.intercept_callback = self.ask_intercept
        self.orchestrator.role_callback = self.update_role
        
        if self.orchestrator.config.multi_agent:
            self.orchestrator.run_multi_agent(self.harness_task)
        else:
            self.orchestrator.run_task(self.harness_task)
        
        if os.environ.get("AUTO_APPROVE") == "1":
            self.exit()
        else:
            self.call_from_thread(self.show_outcome_and_loop)
            
    def show_outcome_and_loop(self):
        def on_outcome_dismissed(_):
            self.prepare_next_task(first_launch=False)
        self.push_screen(OutcomeModal(self.orchestrator.status), on_outcome_dismissed)
        
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

    def update_history_list(self):
        try:
            history_list = self.query_one("#history_list", ListView)
            history_list.clear()
            runs_dir = os.path.join(self.orchestrator.repo_root, "runs")
            if os.path.exists(runs_dir):
                runs = sorted(os.listdir(runs_dir), reverse=True)
                for r in runs:
                    status_path = os.path.join(runs_dir, r, "status.json")
                    if os.path.exists(status_path):
                        import json
                        try:
                            with open(status_path, "r") as f:
                                st = json.load(f)
                                label = f"{r} | {st.get('state')} | Iter: {st.get('iteration')}"
                                item = ListItem(Static(label), id=f"run_{r}")
                                history_list.append(item)
                        except:
                            pass
        except:
            pass

    def on_list_view_selected(self, event: ListView.Selected):
        if event.list_view.id == "history_list":
            run_id = event.item.id[4:]
            runs_dir = os.path.join(self.orchestrator.repo_root, "runs")
            outcome_path = os.path.join(runs_dir, run_id, "OUTCOME.md")
            history_view = self.query_one("#history_view", Markdown)
            if os.path.exists(outcome_path):
                with open(outcome_path, "r") as f:
                    history_view.update(f.read())
