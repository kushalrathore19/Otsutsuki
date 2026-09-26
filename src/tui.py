from textual.app import App, ComposeResult
from textual.containers import VerticalScroll, Horizontal, Vertical
from textual.widgets import Header, Footer, Static, Markdown, Input, Collapsible, Label, TextArea
from textual.binding import Binding
from textual import work
from rich.syntax import Syntax
import sys
import os
import threading
import json
import time

class ChatMessage(Static):
    def __init__(self, role: str, content, role_label=None, is_error=False, markup=False, **kwargs):
        super().__init__(**kwargs)
        self.msg_role = role
        self.content = content
        self.role_label = role_label
        self.is_error = is_error
        self.use_markup = markup
        self.body_static = None

    def compose(self) -> ComposeResult:
        classes = f"chat-msg {self.msg_role}"
        if self.is_error:
            classes += " error"
            
        with Vertical(classes=classes):
            if self.role_label:
                yield Label(self.role_label, classes="role-label")
                
            if self.msg_role == "tool" or self.msg_role == "diff":
                title = "⏺ Tool Call" if self.msg_role == "tool" else "📄 Patch/Diff"
                if isinstance(self.content, tuple):
                    title = self.content[0]
                    body = self.content[1]
                else:
                    body = self.content
                    
                with Collapsible(title=title, collapsed=True):
                    if self.msg_role == "diff":
                        yield Static(Syntax(body, "diff", theme="monokai", word_wrap=True))
                    else:
                        self.body_static = Static(body)
                        yield self.body_static
            else:
                if getattr(self, "use_markup", False) and isinstance(self.content, str):
                    yield Static(self.content, markup=True)
                elif isinstance(self.content, str):
                    yield Markdown(str(self.content))
                else:
                    yield Static(self.content)

    def append_text(self, text):
        if self.body_static:
            old = self.body_static.renderable
            self.body_static.update(f"{old}\n{text}")
            if "Exit Code:" in text and "Exit Code: 0" not in text:
                self.add_class("error")

class MissionControl(App):
    CSS = """
    #transcript {
        height: 1fr;
        overflow-y: auto;
    }
    #bottom_bar {
        dock: bottom;
        height: auto;
        border-top: solid $primary;
        background: $panel;
    }
    #status_bar {
        height: 1;
        background: $boost;
        color: $text;
        padding: 0 1;
    }
    #sys_status {
        height: 1;
        color: $text-muted;
        padding: 0 1;
    }
    #input_hint {
        height: 1;
        color: $text-muted;
        text-style: italic;
        padding: 0 1;
    }
    #input_box {
        width: 100%;
        background: $surface;
        border: none;
        padding: 0 1;
    }
    #input_prompt {
        height: 1;
        color: $success;
        padding: 0 1;
    }
    .chat-msg {
        margin: 1 2;
        padding: 1 2;
        background: $surface;
        border: solid $primary-background;
    }
    .chat-msg.user {
        border: solid $success;
        margin-left: 10;
        color: $text;
    }
    .chat-msg.agent {
        border: none;
        background: transparent;
    }
    .chat-msg.tool, .chat-msg.diff {
        border: solid $accent;
    }
    .chat-msg.verification {
        background: $success;
        color: black;
    }
    .chat-msg.error {
        border: solid $error;
    }
    .chat-msg.verification.error {
        background: $error;
        color: white;
    }
    .role-label {
        text-style: bold;
        color: $warning;
        margin-bottom: 1;
    }
    """
    
    BINDINGS = [
        Binding("q", "quit", "Quit")
    ]
    
    def __init__(self, orchestrator, task: str = None, **kwargs):
        super().__init__(**kwargs)
        self.orchestrator = orchestrator
        self.harness_task = task
        
        self.confirm_event = threading.Event()
        self.confirm_result = False
        
        self.intercept_event = threading.Event()
        self.intercept_result = None
        
        self.input_mode = "task" 
        self.current_role = "Single Agent"
        self.run_active = False
        
        self.log_queue = []
        from logging_setup import setup_logging
        setup_logging(self.log_queue)

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(id="transcript")
        with Vertical(id="bottom_bar"):
            yield Static("Role: Single Agent | Iteration: 0 | Tokens: 0 | Cost: $0.00", id="status_bar")
            yield Static("", id="sys_status", markup=True)
            yield Static("Enter to send", id="input_hint")
            yield Static("❯ ", id="input_prompt")
            yield Input(id="input_box", placeholder="Type a task, or / for commands")
        
    def on_mount(self):
        self.set_interval(0.1, self.update_logs)
        self.set_interval(0.5, self.update_status)
        
        inp = self.query_one("#input_box", Input)
        if self.harness_task:
            if os.environ.get("AUTO_APPROVE") == "1":
                self.run_orchestrator()
            else:
                inp.value = self.harness_task
                
        inp.focus()

        
    def update_logs(self):
        transcript = self.query_one("#transcript", VerticalScroll)
        for msg in self.log_queue:
            msg = msg.strip("\n")
            clean_msg = msg
            for p in ["[red]ERROR[/red] ", "[yellow]WARN[/yellow] ", "[dim]INFO[/dim] ", "[dim]DEBUG[/dim] "]:
                if clean_msg.startswith(p):
                    clean_msg = clean_msg[len(p):]
            clean_msg = re.sub(r'^\[[A-Z]+\]\s*', '', clean_msg)
            
            if clean_msg.startswith("Model response:"):
                self.add_block("agent", clean_msg[15:].strip())
            elif clean_msg.startswith("Executing:"):
                cmd = clean_msg[10:].strip()
                summary = f"⏺ Bash: {cmd[:50]}..." if len(cmd) > 50 else f"⏺ Bash: {cmd}"
                self.last_tool_msg = self.add_block("tool", (summary, f"Command: {cmd}"))
            elif clean_msg.startswith("Exit Code:") or clean_msg.startswith("Stdout:") or clean_msg.startswith("Stderr:"):
                if hasattr(self, "last_tool_msg") and self.last_tool_msg:
                    self.last_tool_msg.append_text(clean_msg)
                else:
                    self.add_block("agent", clean_msg)
            elif clean_msg.startswith("Critique failed!") or clean_msg.startswith("Verifier failed"):
                self.add_block("verification", clean_msg, is_error=True)
            elif "Task completed successfully!" in clean_msg or "Triggering post-patch self-critique" in clean_msg:
                self.add_block("verification", clean_msg, is_error=False)
            elif clean_msg.startswith("--- Iteration") or clean_msg.startswith("Starting"):
                self.add_block("agent", f"**{clean_msg}**")
            elif clean_msg.startswith("Nudge injected:") or clean_msg.startswith("Manual Rollback"):
                pass 
            else:
                if "ERROR" in msg or "WARN" in msg:
                    self.add_block("agent", msg, is_error=True, markup=True)
                else:
                    try:
                        self.query_one("#sys_status", Static).update(msg)
                    except:
                        pass
                
            if "patch -p0 < /tmp/patch.diff" in clean_msg and os.path.exists("/tmp/patch.diff"):
                with open("/tmp/patch.diff", "r") as f:
                    self.add_block("diff", f.read())
                    
        if self.log_queue:
            transcript.scroll_end(animate=False)
            self.log_queue.clear()
            
    def update_status(self):
        st = self.orchestrator.status if hasattr(self.orchestrator, "status") else self.orchestrator.evidence.status
        iterations = st.get("iteration", 0)
        tokens = st.get("tokens", 0)
        cost = (tokens / 1_000_000) * 0.20
        self.query_one("#status_bar", Static).update(f"Role: {self.current_role} | Iteration: {iterations} | Tokens: {tokens} | Cost: ${cost:.6f}")

    def add_block(self, role, content, is_error=False, markup=False):
        label = None
        if self.orchestrator.config.multi_agent and role not in ["user", "verification", "diff"]:
            label = self.current_role
            
        msg = ChatMessage(role, content, role_label=label, is_error=is_error, markup=markup)
        self.query_one("#transcript", VerticalScroll).mount(msg)
        return msg

    def update_role(self, role):
        self.current_role = role
        
    @work(thread=True)
    def run_orchestrator(self):
        self.orchestrator.confirm_callback = self.ask_confirmation
        self.orchestrator.intercept_callback = self.ask_intercept
        self.orchestrator.role_callback = self.update_role
        
        self.run_active = True
        self.input_mode = "nudge"
        
        try:
            if self.orchestrator.config.multi_agent:
                self.orchestrator.run_multi_agent(self.harness_task)
            else:
                self.orchestrator.run_task(self.harness_task)
        finally:
            self.run_active = False
            self.input_mode = "task"
            
        if os.environ.get("AUTO_APPROVE") == "1":
            self.exit()
        else:
            st = self.orchestrator.status if hasattr(self.orchestrator, "status") else self.orchestrator.evidence.status
            self.call_from_thread(self.add_block, "verification", f"Run finished. Status: {st.get('state')}")

    def ask_confirmation(self):
        if os.environ.get("AUTO_APPROVE") == "1":
            return True
            
        self.confirm_event.clear()
        
        msg = "Ready to commit. Reply 'yes' to commit or 'no' to roll back."
        self.call_from_thread(self.add_block, "verification", msg)
        self.input_mode = "confirm"
        
        self.confirm_event.wait()
        return self.confirm_result

    def ask_intercept(self, critique, original_task):
        if os.environ.get("AUTO_APPROVE") == "1":
            return original_task
            
        self.intercept_event.clear()
        
        msg = (f"🚨 Pre-Flight Interception 🚨\n\n"
               f"Original Est. Tokens: {critique.get('Original_Est_Tokens')}\n"
               f"New Est. Tokens: {critique.get('New_Est_Tokens')}\n"
               f"Complexity: {critique.get('Complexity')}\n"
               f"Reason: {critique.get('Recommendation_Reason')}\n\n"
               f"Suggested Task:\n{critique.get('Recommended_Task')}\n\n"
               f"Reply 'yes' to accept recommendation, or 'no' to execute original.")
               
        self.call_from_thread(self.add_block, "verification", msg)
        self.input_mode = "intercept"
        self.intercept_critique = critique
        self.intercept_original = original_task
        
        self.intercept_event.wait()
        return self.intercept_result

    def on_input_submitted(self, event: Input.Submitted) -> None:
        val = event.value.strip()
        event.input.value = ""
        if not val:
            return
        self.handle_input_submission(val)

    def handle_input_submission(self, val):
        if val == "/quit":
            self.exit()
            return
        elif val == "/rollback":
            self.add_block("user", "(Command) /rollback")
            self.orchestrator.rollback()
            self.add_block("verification", "Manual rollback triggered.")
            return
        elif val == "/history":
            self.add_block("user", "(Command) /history")
            self.show_history()
            return
        elif val == "/new":
            if self.run_active:
                self.add_block("error", "Cannot start new task while run is active.")
                return
            self.add_block("user", "── New Task ──")
            self.orchestrator.reset()
            self.harness_task = None
            self.input_mode = "task"
            return
            
        if self.input_mode == "confirm":
            self.add_block("user", val)
            ans = val.lower()
            if ans in ["y", "yes"]:
                self.confirm_result = True
                self.input_mode = "nudge"
                self.confirm_event.set()
            elif ans in ["n", "no"]:
                self.confirm_result = False
                self.input_mode = "nudge"
                self.confirm_event.set()
            else:
                self.add_block("verification", "Please reply 'yes' or 'no'.")
                
        elif self.input_mode == "intercept":
            self.add_block("user", val)
            ans = val.lower()
            if ans in ["y", "yes", "a"]:
                self.intercept_result = self.intercept_critique.get('Recommended_Task')
                self.input_mode = "nudge"
                self.intercept_event.set()
            elif ans in ["n", "no", "b"]:
                self.intercept_result = self.intercept_original
                self.input_mode = "nudge"
                self.intercept_event.set()
            else:
                self.add_block("verification", "Please reply 'yes' or 'no'.")
                
        else:
            if self.run_active:
                self.add_block("user", f"{val} (nudge)")
                self.orchestrator.nudge_queue.append(val)
            else:
                self.add_block("user", val)
                self.harness_task = val
                self.run_orchestrator()

    def show_history(self):
        runs_dir = os.path.join(self.orchestrator.repo_root, "runs")
        if not os.path.exists(runs_dir):
            self.add_block("agent", "No history found.")
            return
            
        runs = sorted(os.listdir(runs_dir), reverse=True)
        out = "### Past Runs:\n"
        for r in runs:
            status_path = os.path.join(runs_dir, r, "status.json")
            if os.path.exists(status_path):
                try:
                    with open(status_path, "r") as f:
                        st = json.load(f)
                        out += f"- **{r}**: {st.get('state')} (Iter: {st.get('iteration')})\n"
                except Exception:
                    pass
        self.add_block("agent", out)
