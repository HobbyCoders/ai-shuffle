"""
Claude Code Python SDK Proxy Service
A FastAPI-based service that provides REST API access to Claude Code CLI
Uses Claude OAuth authentication instead of API keys
"""

import os
import logging
import asyncio
import subprocess
import json
import re
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, HttpUrl
from dotenv import load_dotenv

from auth_helper import ClaudeAuthHelper

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


# Pydantic models for request/response
class Message(BaseModel):
    role: str = Field(..., description="Role of the message sender (user or assistant)")
    content: str = Field(..., description="Content of the message")


class ChatRequest(BaseModel):
    prompt: str = Field(..., description="The prompt/question to send to Claude")
    context: Optional[str] = Field(
        default=None,
        description="Optional context or system message"
    )
    model: Optional[str] = Field(
        default=None,
        description="Claude model to use (optional)"
    )


class ExecuteCommandRequest(BaseModel):
    command: str = Field(..., description="Claude Code command to execute")
    args: Optional[List[str]] = Field(
        default=None,
        description="Additional arguments for the command"
    )
    timeout: Optional[int] = Field(
        default=300,
        description="Command timeout in seconds"
    )


class ChatResponse(BaseModel):
    response: str
    status: str
    metadata: Optional[Dict[str, Any]] = None


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    authenticated: bool


class LoginResponse(BaseModel):
    status: str
    message: str
    instructions: Optional[List[str]] = None


# Advanced AI interaction models
class StructuredPromptRequest(BaseModel):
    """
    Advanced prompting with system instructions and optional context.
    Use this for more controlled AI responses.
    """
    user_prompt: str = Field(..., description="The main user prompt/question")
    system_prompt: Optional[str] = Field(
        None,
        description="System instructions to guide Claude's behavior and response format"
    )
    context: Optional[str] = Field(
        None,
        description="Additional context (HTML, text, code, etc.) for Claude to analyze"
    )
    model: Optional[str] = Field(None, description="Claude model to use")
    temperature: Optional[float] = Field(
        None,
        ge=0.0,
        le=1.0,
        description="Response randomness (0.0 = deterministic, 1.0 = creative)"
    )
    max_tokens: Optional[int] = Field(
        None,
        gt=0,
        description="Maximum tokens in response"
    )
    json_mode: bool = Field(
        False,
        description="Attempt to parse response as JSON and return structured data"
    )


class StructuredPromptResponse(BaseModel):
    """Response from structured prompt"""
    success: bool
    response: str = Field(..., description="Claude's text response")
    parsed_json: Optional[Dict[str, Any]] = Field(
        None,
        description="Parsed JSON if json_mode=true and response contains valid JSON"
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Metadata about the response (model, tokens, etc.)"
    )
    error: Optional[str] = None


class ConversationMessage(BaseModel):
    """Single message in a conversation"""
    role: str = Field(..., description="'user' or 'assistant'")
    content: str = Field(..., description="Message content")


class ConversationRequest(BaseModel):
    """
    Multi-turn conversation with context retention.
    Send entire conversation history for context-aware responses.
    """
    messages: List[ConversationMessage] = Field(
        ...,
        description="Conversation history (oldest to newest)"
    )
    system_prompt: Optional[str] = Field(
        None,
        description="System instructions for the entire conversation"
    )
    model: Optional[str] = Field(None, description="Claude model to use")
    temperature: Optional[float] = Field(None, ge=0.0, le=1.0)
    max_tokens: Optional[int] = Field(None, gt=0)


class FileAnalysisRequest(BaseModel):
    """
    Analyze file content (HTML, code, text, etc.)
    """
    content: str = Field(..., description="File content to analyze")
    content_type: str = Field(
        ...,
        description="Type of content: 'html', 'json', 'xml', 'code', 'text', etc."
    )
    analysis_instructions: str = Field(
        ...,
        description="What you want Claude to do with this content"
    )
    output_format: Optional[str] = Field(
        None,
        description="Desired output format: 'json', 'markdown', 'text', 'list'"
    )
    model: Optional[str] = Field(None)


# Global auth helper
auth_helper: Optional[ClaudeAuthHelper] = None


# Utility functions for AI responses
def extract_json_from_response(text: str) -> Optional[Dict[str, Any]]:
    """
    Extract JSON from Claude's response, handling markdown code blocks.
    Returns None if no valid JSON found.
    """
    # Try to parse the entire response as JSON first
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    # Look for JSON in markdown code blocks
    json_patterns = [
        r'```json\s*([\s\S]*?)\s*```',
        r'```\s*([\s\S]*?)\s*```',
        r'\{[\s\S]*\}',  # Any JSON object
        r'\[[\s\S]*\]',  # Any JSON array
    ]

    for pattern in json_patterns:
        matches = re.findall(pattern, text, re.MULTILINE)
        for match in matches:
            try:
                return json.loads(match.strip())
            except json.JSONDecodeError:
                continue

    return None


def build_structured_prompt(
    user_prompt: str,
    system_prompt: Optional[str] = None,
    context: Optional[str] = None,
    output_format: Optional[str] = None
) -> str:
    """Build a comprehensive prompt from components"""
    parts = []

    if system_prompt:
        parts.append(f"SYSTEM INSTRUCTIONS:\n{system_prompt}\n")

    if context:
        # Truncate context if too long (prevent token limits)
        max_context_length = 50000
        if len(context) > max_context_length:
            context = context[:max_context_length] + "\n... (truncated)"
        parts.append(f"CONTEXT/DATA:\n{context}\n")

    if output_format:
        format_instructions = {
            "json": "Return your response as valid JSON only. No markdown, no explanation.",
            "markdown": "Format your response in clean markdown.",
            "list": "Return your response as a numbered or bulleted list.",
            "text": "Return plain text without formatting."
        }
        if output_format.lower() in format_instructions:
            parts.append(f"OUTPUT FORMAT: {format_instructions[output_format.lower()]}\n")

    parts.append(f"USER REQUEST:\n{user_prompt}")

    return "\n".join(parts)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler"""
    global auth_helper

    # Startup
    logger.info("Initializing Claude Code SDK service...")
    auth_helper = ClaudeAuthHelper()

    # Check authentication status
    if auth_helper.is_authenticated():
        logger.info("✓ Authenticated with Claude Code")
    else:
        logger.warning("⚠ Not authenticated with Claude Code. Please login.")
        logger.warning("Run: docker exec -it claude-sdk-agent claude login")

    yield

    # Shutdown
    logger.info("Shutting down Claude Code SDK service")


# Initialize FastAPI app
app = FastAPI(
    title="Claude Code Python SDK Proxy",
    description="REST API wrapper for Claude Code CLI with OAuth authentication. "
                "Provides general-purpose AI capabilities including structured prompting, "
                "file analysis, JSON parsing, and multi-turn conversations.",
    version="2.1.0",
    lifespan=lifespan
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure based on your security needs
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def execute_claude_command(
    command: List[str],
    timeout: int = 300,
    input_text: Optional[str] = None
) -> Dict[str, Any]:
    """
    Execute a Claude Code command and return the result

    Args:
        command: Command and arguments as list
        timeout: Timeout in seconds
        input_text: Optional input to send to the command

    Returns:
        Dict with command output and status
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE if input_text else None
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(input=input_text.encode() if input_text else None),
                timeout=timeout
            )

            return {
                "status": "success" if process.returncode == 0 else "error",
                "returncode": process.returncode,
                "stdout": stdout.decode('utf-8', errors='replace'),
                "stderr": stderr.decode('utf-8', errors='replace')
            }

        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return {
                "status": "timeout",
                "returncode": -1,
                "stdout": "",
                "stderr": f"Command timed out after {timeout} seconds"
            }

    except Exception as e:
        logger.error(f"Error executing command: {e}")
        return {
            "status": "error",
            "returncode": -1,
            "stdout": "",
            "stderr": str(e)
        }


@app.get("/", response_model=HealthResponse)
async def root():
    """Root endpoint - health check with auth status"""
    is_auth = auth_helper.is_authenticated() if auth_helper else False

    return {
        "status": "healthy",
        "service": os.getenv("SERVICE_NAME", "claude-code-sdk"),
        "version": "2.0.0",
        "authenticated": is_auth
    }


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint"""
    if auth_helper is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth helper not initialized"
        )

    is_auth = auth_helper.is_authenticated()

    return {
        "status": "healthy",
        "service": os.getenv("SERVICE_NAME", "claude-code-sdk"),
        "version": "2.0.0",
        "authenticated": is_auth
    }


@app.get("/auth/status")
async def auth_status():
    """Get authentication status"""
    if auth_helper is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth helper not initialized"
        )

    return auth_helper.get_auth_info()


@app.get("/auth/login-instructions")
async def get_login_instructions():
    """Get instructions for logging in"""
    if auth_helper is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth helper not initialized"
        )

    return await auth_helper.get_login_instructions()


@app.post("/auth/login")
async def login():
    """
    Initiate Claude Code login process

    Note: This is an interactive process. For full functionality,
    you need to exec into the container and run 'claude login' manually.
    """
    if auth_helper is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth helper not initialized"
        )

    result = await auth_helper.initiate_login()
    return result


@app.post("/auth/logout")
async def logout():
    """Logout from Claude Code"""
    if auth_helper is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth helper not initialized"
        )

    result = await auth_helper.logout()
    return result


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """
    Send a prompt to Claude Code and get a response

    This uses the Claude Code CLI to interact with Claude via OAuth
    """
    if auth_helper is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth helper not initialized"
        )

    if not auth_helper.is_authenticated():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated with Claude Code. Please login first using /auth/login-instructions"
        )

    try:
        # Build the Claude Code command
        command = ['claude', 'chat']

        if request.model:
            command.extend(['--model', request.model])

        # Execute the command with the prompt as input
        result = await execute_claude_command(
            command=command,
            timeout=int(os.getenv("COMMAND_TIMEOUT", "300")),
            input_text=request.prompt
        )

        if result['status'] == 'success':
            return {
                "response": result['stdout'],
                "status": "success",
                "metadata": {
                    "model": request.model or "default",
                    "returncode": result['returncode']
                }
            }
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Claude Code error: {result['stderr']}"
            )

    except Exception as e:
        logger.error(f"Error calling Claude Code: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error communicating with Claude Code: {str(e)}"
        )


@app.post("/execute")
async def execute_command(request: ExecuteCommandRequest):
    """
    Execute a custom Claude Code command

    This allows you to run any Claude Code CLI command
    """
    if auth_helper is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth helper not initialized"
        )

    if not auth_helper.is_authenticated():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated with Claude Code. Please login first."
        )

    try:
        # Build command
        command = ['claude', request.command]
        if request.args:
            command.extend(request.args)

        # Execute
        result = await execute_claude_command(
            command=command,
            timeout=request.timeout or 300
        )

        return {
            "command": ' '.join(command),
            "status": result['status'],
            "returncode": result['returncode'],
            "output": result['stdout'],
            "error": result['stderr']
        }

    except Exception as e:
        logger.error(f"Error executing command: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error executing command: {str(e)}"
        )


@app.get("/version")
async def get_claude_version():
    """Get Claude Code version"""
    result = await execute_claude_command(['claude', '--version'], timeout=10)

    if result['status'] == 'success':
        return {
            "version": result['stdout'].strip(),
            "status": "success"
        }
    else:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get Claude Code version"
        )


@app.post("/prompt/structured", response_model=StructuredPromptResponse)
async def structured_prompt(request: StructuredPromptRequest):
    """
    Advanced prompting with system instructions, context, and optional JSON parsing.

    This is the most flexible endpoint - use it when you need:
    - System-level instructions to guide Claude's behavior
    - Large context (HTML, code, documents)
    - Structured JSON responses
    - Control over temperature and tokens

    Example for Kavita manga scraping:
    {
        "user_prompt": "Extract series metadata and chapter list from this HTML",
        "system_prompt": "You are a manga metadata extractor. Return valid JSON only.",
        "context": "<html>...</html>",
        "json_mode": true
    }
    """
    if not auth_helper or not auth_helper.is_authenticated():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated. Please login first."
        )

    try:
        # Build the full prompt
        full_prompt = build_structured_prompt(
            user_prompt=request.user_prompt,
            system_prompt=request.system_prompt,
            context=request.context,
            output_format="json" if request.json_mode else None
        )

        # Build Claude command
        command = ['claude', 'chat']
        if request.model:
            command.extend(['--model', request.model])

        # Execute
        result = await execute_claude_command(
            command=command,
            timeout=int(os.getenv("COMMAND_TIMEOUT", "300")),
            input_text=full_prompt
        )

        if result['status'] != 'success':
            return StructuredPromptResponse(
                success=False,
                response="",
                error=f"Claude execution failed: {result['stderr']}"
            )

        response_text = result['stdout'].strip()

        # Try to extract JSON if requested
        parsed_json = None
        if request.json_mode:
            parsed_json = extract_json_from_response(response_text)

        return StructuredPromptResponse(
            success=True,
            response=response_text,
            parsed_json=parsed_json,
            metadata={
                "model": request.model or "default",
                "json_parsed": parsed_json is not None,
                "response_length": len(response_text)
            }
        )

    except Exception as e:
        logger.error(f"Error in structured_prompt: {str(e)}")
        return StructuredPromptResponse(
            success=False,
            response="",
            error=str(e)
        )


@app.post("/analyze/file", response_model=StructuredPromptResponse)
async def analyze_file(request: FileAnalysisRequest):
    """
    Analyze file content (HTML, JSON, code, etc.) with specific instructions.

    Useful for:
    - HTML parsing and data extraction
    - Code analysis
    - Document understanding
    - JSON/XML transformation

    Example:
    {
        "content": "<html>...</html>",
        "content_type": "html",
        "analysis_instructions": "Extract all links and their titles",
        "output_format": "json"
    }
    """
    if not auth_helper or not auth_helper.is_authenticated():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated. Please login first."
        )

    try:
        # Build system prompt based on content type
        system_prompts = {
            "html": "You are an expert at parsing and analyzing HTML. Extract structured data accurately.",
            "json": "You are a JSON analysis expert. Parse and transform JSON data as requested.",
            "xml": "You are an XML parsing expert. Extract and structure data from XML.",
            "code": "You are a code analysis expert. Analyze code structure, functionality, and quality.",
            "text": "You are a text analysis expert. Extract insights and information from text."
        }

        system_prompt = system_prompts.get(request.content_type.lower(),
                                          "Analyze the provided content carefully.")

        # Build structured prompt
        full_prompt = build_structured_prompt(
            user_prompt=request.analysis_instructions,
            system_prompt=system_prompt,
            context=request.content,
            output_format=request.output_format
        )

        # Execute
        command = ['claude', 'chat']
        if request.model:
            command.extend(['--model', request.model])

        result = await execute_claude_command(
            command=command,
            timeout=int(os.getenv("COMMAND_TIMEOUT", "300")),
            input_text=full_prompt
        )

        if result['status'] != 'success':
            return StructuredPromptResponse(
                success=False,
                response="",
                error=f"Analysis failed: {result['stderr']}"
            )

        response_text = result['stdout'].strip()

        # Try to parse JSON if output format is json
        parsed_json = None
        if request.output_format and request.output_format.lower() == "json":
            parsed_json = extract_json_from_response(response_text)

        return StructuredPromptResponse(
            success=True,
            response=response_text,
            parsed_json=parsed_json,
            metadata={
                "content_type": request.content_type,
                "content_length": len(request.content),
                "output_format": request.output_format
            }
        )

    except Exception as e:
        logger.error(f"Error in analyze_file: {str(e)}")
        return StructuredPromptResponse(
            success=False,
            response="",
            error=str(e)
        )


@app.post("/conversation", response_model=ChatResponse)
async def multi_turn_conversation(request: ConversationRequest):
    """
    Multi-turn conversation with full context retention.

    Send the entire conversation history for context-aware responses.
    Useful for:
    - Chatbots
    - Interactive assistants
    - Contextual Q&A

    Example:
    {
        "messages": [
            {"role": "user", "content": "What is Python?"},
            {"role": "assistant", "content": "Python is a programming language..."},
            {"role": "user", "content": "What can I build with it?"}
        ],
        "system_prompt": "You are a helpful programming tutor."
    }
    """
    if not auth_helper or not auth_helper.is_authenticated():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated. Please login first."
        )

    try:
        # Build conversation context
        conversation_parts = []

        if request.system_prompt:
            conversation_parts.append(f"SYSTEM: {request.system_prompt}\n")

        conversation_parts.append("CONVERSATION HISTORY:")
        for msg in request.messages:
            role = msg.role.upper()
            conversation_parts.append(f"{role}: {msg.content}")

        full_conversation = "\n".join(conversation_parts)

        # Execute
        command = ['claude', 'chat']
        if request.model:
            command.extend(['--model', request.model])

        result = await execute_claude_command(
            command=command,
            timeout=int(os.getenv("COMMAND_TIMEOUT", "300")),
            input_text=full_conversation
        )

        if result['status'] == 'success':
            return {
                "response": result['stdout'].strip(),
                "status": "success",
                "metadata": {
                    "model": request.model or "default",
                    "message_count": len(request.messages)
                }
            }
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Conversation failed: {result['stderr']}"
            )

    except Exception as e:
        logger.error(f"Error in conversation: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error in conversation: {str(e)}"
        )


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))

    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=False,
        log_level=os.getenv("LOG_LEVEL", "info").lower()
    )
