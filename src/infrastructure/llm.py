import json
import re
import sys
import logging
from typing import List, Dict, Any, Optional
import httpx
from openai import AsyncOpenAI, APIConnectionError
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type
)

from ..config import Settings
from ..core.exceptions import LLMError, NetworkError
from ..core.models import AgentAction, ConversationMessage

logger = logging.getLogger(__name__)


class LLMService:
    
    def __init__(self, settings: Settings):
        self.settings = settings
        self._http_client: Optional[httpx.AsyncClient] = None
        
        if settings.proxy_url:
            self._http_client = httpx.AsyncClient(
                proxy=settings.proxy_url,
                timeout=httpx.Timeout(settings.http_timeout)
            )
        
        self.client = AsyncOpenAI(
            api_key=settings.api_key,
            base_url=settings.api_base_url,
            http_client=self._http_client
        )
        
        self._connection_verified = False
    
    async def close(self) -> None:
        """Close HTTP client if it exists."""
        if self._http_client:
            await self._http_client.aclose()
    
    async def __aenter__(self) -> 'LLMService':
        """Context manager entry."""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit with cleanup."""
        await self.close()
        return False
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((NetworkError, httpx.TimeoutException))
    )

    async def generate_action(
            self,
            messages: List[Dict[str, str]],
            temperature: float = 0.1
        ) -> AgentAction:
            try:
                response = await self.client.chat.completions.create(
                    model=self.settings.model_name,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=self.settings.max_tokens
                )
            except httpx.TimeoutException as e:
                raise NetworkError(f"Timeout connecting to LLM: {e}") from e
            except Exception as e:
                raise LLMError(f"LLM request failed: {e}", model_name=self.settings.model_name) from e

            
            self._connection_verified = True
            
            content = response.choices[0].message.content

            if not content or not content.strip():
                print(f"[WARNING] Model {self.settings.model_name} returned empty response")
                raise LLMError(
                    f"Empty response from model {self.settings.model_name}",
                    model_name=self.settings.model_name
                )

            
            json_str = self._extract_json_from_response(content)
            
            if not json_str:
                raise LLMError(
                    f"No valid JSON found in LLM response. Content: {content[:200]}",
                    model_name=self.settings.model_name
                )
            
            try:
                action_dict = json.loads(json_str)
            except json.JSONDecodeError as e:
                print(f"[ERROR] Failed to decode JSON from model {self.settings.model_name}: {e}")
                raise LLMError(f"JSON decode error: {e}", model_name=self.settings.model_name)
            
            action = AgentAction.model_validate(action_dict)
            
            return action
    
    def _extract_json_from_response(self, content: str) -> str:
        if not content:
            return ""
        
        code_block_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', content, re.DOTALL)
        if code_block_match:
            try:
                candidate = code_block_match.group(1).strip()
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError as e:
                logger.debug(f"Failed to parse JSON from code block: {e}")
                pass

        first_brace = content.find('{')
        last_brace = content.rfind('}')
        
        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            potential_json = content[first_brace:last_brace + 1]
            potential_json = re.sub(r',\s*}', '}', potential_json) 
            potential_json = potential_json.replace('’', "'").replace('“', '"').replace('”', '"')
            try:
                potential_json = potential_json.strip()
                json.loads(potential_json)
                return potential_json
            except json.JSONDecodeError:
                try:
                    cleaned_potential = re.sub(r'\n', ' ', potential_json)
                    json.loads(cleaned_potential)
                    return cleaned_potential
                except json.JSONDecodeError as e:
                    logger.debug(f"Failed to parse cleaned JSON: {e}")
                    pass
        
        try:
            cleaned = re.sub(r'^[^{]*', '', content)
            cleaned = re.sub(r'[^}]*$', '', cleaned)
            json.loads(cleaned)
            return cleaned
        except json.JSONDecodeError as e:
            logger.debug(f"Failed to parse aggressively cleaned JSON: {e}")
            pass

        return ""