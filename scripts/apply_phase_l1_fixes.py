import re

def update_context():
    with open('backend/app/services/conversation_context.py', 'r', encoding='utf-8') as f:
        c = f.read()

    # Fix LANGUAGE_CHANGE block to set explicit language preference
    old_lang = '''            ctx.language = new_target_lang
            return self._response(msg, "LANGUAGE_CHANGE", lang=new_target_lang, new_language=new_target_lang)'''

    new_lang = '''            ctx.language = new_target_lang
            ctx.has_explicit_language_preference = True
            ctx.explicit_language = new_target_lang
            return self._response(msg, "LANGUAGE_CHANGE", lang=new_target_lang, new_language=new_target_lang)'''
    c = c.replace(old_lang, new_lang)

    # Wrap process_turn to guarantee last_response_text is always saved for every substantive response
    if 'def _process_turn_internal(' not in c:
        c = c.replace('    def process_turn(', '    def _process_turn_internal(')
        wrapper = '''    def process_turn(
        self,
        session_id: str,
        user_text: str,
        language: str = "mr-IN",
        turn_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """Processes turn and ensures last_response_text is tracked across all response pathways."""
        res = self._process_turn_internal(session_id, user_text, language=language, turn_id=turn_id)
        ctx = self.get_context(session_id)
        if ctx and res.get("intent") != "REPEAT_QUERY":
            ctx.last_response_text = res.get("response_text")
        return res

'''
        c = c.replace('    def _process_turn_internal(', wrapper + '    def _process_turn_internal(')

    with open('backend/app/services/conversation_context.py', 'w', encoding='utf-8') as f:
        f.write(c)

    print("update_context completed.")

if __name__ == '__main__':
    update_context()
