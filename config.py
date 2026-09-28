# config.py

import os

from dotenv import load_dotenv
from openai import AsyncOpenAI
from agents import (
    OpenAIChatCompletionsModel,
    set_tracing_disabled,
)

load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

if not OPENROUTER_API_KEY:
    raise RuntimeError("OPENROUTER_API_KEY is not set")

openrouter_client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

# You don't have an OpenAI API key for OpenAI tracing
set_tracing_disabled(True)


def get_model(model_name: str):
    return OpenAIChatCompletionsModel(
        model=model_name,
        openai_client=openrouter_client,
    )