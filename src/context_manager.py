import logging
class ContextManager:
    def __init__(self, initial_messages=None):
        self.messages = initial_messages or []
    def append(self, message):
        self.messages.append(message)
    def extend(self, messages):
        self.messages.extend(messages)
    def prune_and_note(self, failure_summary):
        # One assistant turn may carry several tool calls (each with a
        # response), so pop *every* trailing tool message before removing
        # the assistant turn that requested them.
        while self.messages and self.messages[-1].get("role") == "tool":
            self.messages.pop()
        if self.messages and self.messages[-1].get("role") == "assistant":
            self.messages.pop()
        self.messages.append({"role": "user", "content": failure_summary})
        logging.info(f"Context pruned. Total messages now: {len(self.messages)}")
    def get_messages(self):
        return self.messages
