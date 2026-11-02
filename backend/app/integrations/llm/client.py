"""
LLM Client for CityLinc AI Voice Agent.
Supports Sarvam LLM, OpenAI, Groq, and CityLinc local transit knowledge engine.
"""
import logging
from typing import List, Dict, Any, Optional
import httpx
from backend.app.core.config import settings
from backend.app.services.citylinc_knowledge import knowledge_service

logger = logging.getLogger("citylinc.llm")

class LLMClient:
    """Orchestrates multilingual completions for CityLinc Transit Assistant."""

    def __init__(self):
        self.sarvam_key = settings.SARVAM_API_KEY
        self.openai_key = settings.OPENAI_API_KEY
        self.groq_key = settings.GROQ_API_KEY

    async def generate_response(
        self,
        messages: List[Dict[str, str]],
        language_code: str = "mr-IN"
    ) -> str:
        """
        Generates conversational response maintaining context and language.
        Prioritizes:
        1. Direct factual transit queries (fares, timetables, routes)
        2. Sarvam Chat Completions API
        3. OpenAI / Groq fallback
        4. Context-aware multilingual fallback
        """
        user_message = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_message = m.get("content", "")
                break

        # Check local knowledge base first for rapid, exact transit facts
        local_ans = knowledge_service.answer_query_locally(user_message, language=language_code)
        if local_ans:
            logger.info(f"Local transit knowledge match for query: '{user_message[:30]}...' -> '{local_ans[:30]}...'")
            return local_ans

        # Extract focused context from the complete 315-route knowledge base
        relevant_context = knowledge_service.retrieve_relevant_context(user_message)
        system_prompt = knowledge_service.get_system_prompt(language_code, relevant_context=relevant_context)

        # Try Sarvam LLM if key is present
        if self.sarvam_key and len(self.sarvam_key) > 8:
            try:
                full_messages = [{"role": "system", "content": system_prompt}] + messages[-6:]
                
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(
                        f"{settings.SARVAM_BASE_URL}/v1/chat/completions",
                        headers={
                            "api-subscription-key": self.sarvam_key,
                            "Content-Type": "application/json"
                        },
                        json={
                            "model": "sarvam-105b-conversations",
                            "messages": full_messages,
                            "temperature": 0.5,
                            "max_tokens": 150
                        }
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        reply = data["choices"][0]["message"]["content"].strip()
                        return reply
            except Exception as e:
                logger.warning(f"Sarvam LLM call failed, trying next provider: {e}")

        # Try Groq if configured
        if self.groq_key:
            try:
                full_messages = [{"role": "system", "content": system_prompt}] + messages[-6:]
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(
                        "https://api.groq.com/openai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {self.groq_key}"},
                        json={
                            "model": "llama-3.3-70b-versatile",
                            "messages": full_messages,
                            "temperature": 0.4,
                            "max_tokens": 120
                        }
                    )
                    if resp.status_code == 200:
                        return resp.json()["choices"][0]["message"]["content"].strip()
            except Exception as e:
                logger.warning(f"Groq LLM call failed: {e}")

        # Try OpenAI if configured
        if self.openai_key:
            try:
                system_prompt = knowledge_service.get_system_prompt(language_code)
                full_messages = [{"role": "system", "content": system_prompt}] + messages[-6:]
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(
                        "https://api.openai.com/v1/chat/completions",
                        headers={"Authorization": f"Bearer {self.openai_key}"},
                        json={
                            "model": "gpt-4o-mini",
                            "messages": full_messages,
                            "temperature": 0.5,
                            "max_tokens": 120
                        }
                    )
                    if resp.status_code == 200:
                        return resp.json()["choices"][0]["message"]["content"].strip()
            except Exception as e:
                logger.warning(f"OpenAI LLM call failed: {e}")

        # Graceful multilingual default response
        if language_code == "mr-IN":
            return f"मी सिटीलिंक नाशिक बस सहाय्यक आहे. मी तुमच्या '{user_message}' या प्रश्नाबद्दल बस मार्ग, वेळ किंवा पासची माहिती शोधत आहे. कृपया बस क्रमांक सांगा."
        elif language_code == "hi-IN":
            return f"मैं सिटीलिंक नाशिक बस सहायक हूँ। मुझे आपके प्रश्न '{user_message}' के लिए बस रूट या समय बताएं।"
        else:
            return f"I am your CityLinc Nashik bus assistant. Regarding '{user_message}', please tell me your desired route number or bus stop."

# Global singleton
llm_client = LLMClient()

