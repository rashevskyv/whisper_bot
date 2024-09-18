import time
from collections import deque

class ContextManager:
    def __init__(self):
        self.contexts = {}
        self.default_context_duration = 24 * 60 * 60  # 24 hours in seconds

    def add_message(self, user_id, role, content):
        current_time = time.time()
        if user_id not in self.contexts:
            self.contexts[user_id] = {
                'messages': deque(),
                'duration': self.default_context_duration
            }
        
        self.contexts[user_id]['messages'].append({
            'role': role,
            'content': content,
            'timestamp': current_time
        })
        
        self._clean_old_messages(user_id)

    def get_context(self, user_id):
        if user_id in self.contexts:
            return [msg for msg in self.contexts[user_id]['messages'] if msg['role'] != 'system']
        return []

    def clear_context(self, user_id):
        if user_id in self.contexts:
            self.contexts[user_id]['messages'].clear()

    def set_context_duration(self, user_id, duration):
        if user_id in self.contexts:
            self.contexts[user_id]['duration'] = duration
        else:
            self.contexts[user_id] = {
                'messages': deque(),
                'duration': duration
            }

    def _clean_old_messages(self, user_id):
        if user_id in self.contexts:
            current_time = time.time()
            context = self.contexts[user_id]
            while context['messages'] and current_time - context['messages'][0]['timestamp'] > context['duration']:
                context['messages'].popleft()

context_manager = ContextManager()