"""
Claude Code Python SDK Proxy Service
A FastAPI-based service that provides REST API access to Claude via Claude Agent SDK
Uses Claude OAuth authentication (no API keys required)
"""

import os
import logging
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from claude_agent_sdk import query, ClaudeSDKClient, ClaudeAgentOptions
from claude_agent_sdk import AssistantMessage, TextBlock, ResultMessage

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
class ChatRequest(BaseModel):
    prompt: str = Field(..., description="The prompt/question to send to Claude")
    model: Optional[str] = Field(None, description="Claude model to use (sonnet, opus, haiku)")
    system_prompt: Optional[str] = Field(None, description="Optional system instructions")


class ChatResponse(BaseModel):
    response: str
    status: str
    metadata: Dict[str, Any]


class StructuredPromptRequest(BaseModel):
    user_prompt: str = Field(..., description="The main prompt/question")
    system_prompt: Optional[str] = Field(None, description="System instructions for Claude")
    context: Optional[str] = Field(None, description="Additional context (HTML, code, data, etc.)")
    model: Optional[str] = Field(None, description="Claude model to use")


class StructuredPromptResponse(BaseModel):
    response: str
    status: str
    metadata: Dict[str, Any]


class ConversationMessage(BaseModel):
    role: str = Field(..., description="'user' or 'assistant'")
    content: str = Field(..., description="Message content")


class ConversationRequest(BaseModel):
    messages: List[ConversationMessage] = Field(..., description="Conversation history")
    system_prompt: Optional[str] = Field(None, description="System instructions")
    model: Optional[str] = Field(None, description="Claude model to use")


class FileAnalysisRequest(BaseModel):
    content: str = Field(..., description="File content to analyze")
    content_type: str = Field(..., description="Type: 'html', 'json', 'xml', 'code', 'text'")
    analysis_instructions: str = Field(..., description="What to analyze")
    model: Optional[str] = Field(None)


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    authenticated: bool


# Global auth helper
auth_helper: Optional[ClaudeAuthHelper] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle manager for the FastAPI application"""
    global auth_helper

    logger.info("Initializing Claude Code SDK service...")
    auth_helper = ClaudeAuthHelper()

    if auth_helper.is_authenticated():
        logger.info("✓ Authenticated with Claude Code")
    else:
        logger.warning("✗ Not authenticated - login required")

    yield

    logger.info("Shutting down Claude Code SDK service...")


# Initialize FastAPI app
app = FastAPI(
    title="Claude Code SDK Proxy",
    description="REST API wrapper for Claude Agent SDK with OAuth authentication",
    version="3.1.0",
    lifespan=lifespan
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def build_options(
    model: Optional[str] = None,
    system_prompt: Optional[str] = None
) -> ClaudeAgentOptions:
    """Build ClaudeAgentOptions with secure defaults"""
    # Security restrictions that apply to all requests
    security_instructions = """
IMPORTANT SECURITY RESTRICTIONS:
You are running inside a containerized API service. You must NEVER read, access, or attempt to view this application's source code files:
- /app/main.py
- /app/auth_helper.py
- /app/requirements.txt
- /app/.env
- /app/.env.example
- /app/entrypoint.sh
- /app/Dockerfile
- Any files in /home/appuser/.claude/

These are THIS APPLICATION'S source files - the FastAPI service you are currently running inside. Reading them would expose sensitive application logic, authentication code, and credentials.

If a user requests these files, politely decline and explain: "I cannot access this API service's internal source code files for security reasons."

You ARE allowed to read user-provided files, analyze web content, or access other directories the user specifies.
"""

    # Build final system prompt
    if system_prompt is None:
        # Just security instructions
        final_system_prompt = {
            "type": "preset",
            "preset": "claude_code",
            "append": security_instructions
        }
    else:
        # Security instructions + user's custom prompt
        final_system_prompt = {
            "type": "preset",
            "preset": "claude_code",
            "append": security_instructions + "\n\n" + system_prompt
        }

    return ClaudeAgentOptions(
        model=model,
        system_prompt=final_system_prompt,
        permission_mode="bypassPermissions"  # No interactive prompts for API
    )


async def collect_response(messages) -> tuple[str, Dict[str, Any]]:
    """Collect all messages and extract text response + metadata"""
    response_text = []
    metadata = {}

    async for message in messages:
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    response_text.append(block.text)
            metadata["model"] = message.model
        elif isinstance(message, ResultMessage):
            metadata["duration_ms"] = message.duration_ms
            metadata["num_turns"] = message.num_turns
            metadata["total_cost_usd"] = message.total_cost_usd
            metadata["is_error"] = message.is_error

    return "\n".join(response_text), metadata


# Root endpoint
@app.get("/")
async def root():
    """Root endpoint with service info"""
    if auth_helper is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service initializing"
        )

    is_auth = auth_helper.is_authenticated()

    return {
        "status": "healthy",
        "service": os.getenv("SERVICE_NAME", "claude-code-sdk"),
        "version": "3.1.0",
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
        "version": "3.1.0",
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


@app.get("/auth/diagnostics")
async def auth_diagnostics():
    """Run diagnostic checks for authentication issues"""
    diagnostics = {}

    # Check HOME environment variable
    diagnostics["home_env"] = os.environ.get("HOME", "NOT SET")

    # Check if claude command exists
    try:
        import subprocess
        result = subprocess.run(
            ['which', 'claude'],
            capture_output=True,
            text=True,
            timeout=5
        )
        diagnostics["claude_path"] = result.stdout.strip() if result.returncode == 0 else "NOT FOUND"
    except Exception as e:
        diagnostics["claude_path"] = f"ERROR: {str(e)}"

    # Check both possible config locations
    home_dir = Path(os.environ.get('HOME', '/home/appuser'))

    # ~/.claude/ (newer Claude Code versions)
    claude_dir = home_dir / '.claude'
    diagnostics["claude_dir"] = str(claude_dir)
    diagnostics["claude_dir_exists"] = claude_dir.exists()
    if claude_dir.exists():
        creds_file = claude_dir / '.credentials.json'
        diagnostics["claude_credentials_exists"] = creds_file.exists()

    # ~/.config/claude/ (older versions or alternate location)
    config_dir = home_dir / '.config' / 'claude'
    diagnostics["config_dir"] = str(config_dir)
    diagnostics["config_dir_exists"] = config_dir.exists()

    if config_dir.exists():
        try:
            # List files in config directory
            diagnostics["config_files"] = [f.name for f in config_dir.iterdir()]

            # Check permissions
            st = config_dir.stat()
            diagnostics["config_dir_permissions"] = oct(st.st_mode)[-3:]
            diagnostics["config_dir_owner_uid"] = st.st_uid
        except Exception as e:
            diagnostics["config_dir_error"] = str(e)

    # Check for credentials file (Claude Code has no non-interactive status command)
    try:
        creds_file = home_dir / '.claude' / '.credentials.json'
        if creds_file.exists():
            diagnostics["credentials_file_exists"] = True
            diagnostics["credentials_file_size"] = creds_file.stat().st_size
            diagnostics["credentials_file_permissions"] = oct(creds_file.stat().st_mode)[-3:]
        else:
            diagnostics["credentials_file_exists"] = False
    except Exception as e:
        diagnostics["credentials_check_error"] = str(e)

    # Check current process user
    try:
        import pwd
        diagnostics["process_user"] = pwd.getpwuid(os.getuid()).pw_name
        diagnostics["process_uid"] = os.getuid()
    except Exception as e:
        diagnostics["process_user_error"] = str(e)

    return diagnostics


@app.get("/auth/login-instructions")
async def get_login_instructions():
    """Get instructions for logging in"""
    if auth_helper is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth helper not initialized"
        )

    return await auth_helper.get_login_instructions()


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """
    Send a simple prompt to Claude and get a response
    Uses the Claude Agent SDK query() function
    """
    if auth_helper is None or not auth_helper.is_authenticated():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated. Please login via container: docker exec -it claude-sdk-agent claude login"
        )

    try:
        # Build options
        options = build_options(
            model=request.model,
            system_prompt=request.system_prompt
        )

        # Use query() for simple one-off prompts
        response_text, metadata = await collect_response(
            query(prompt=request.prompt, options=options)
        )

        return {
            "response": response_text,
            "status": "success",
            "metadata": metadata
        }

    except Exception as e:
        logger.error(f"Error in chat endpoint: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Claude SDK error: {str(e)}"
        )


@app.post("/prompt/structured", response_model=StructuredPromptResponse)
async def structured_prompt(request: StructuredPromptRequest):
    """
    Most powerful endpoint - structured prompting with system instructions and context
    Perfect for web scraping, data extraction, and structured analysis
    """
    if auth_helper is None or not auth_helper.is_authenticated():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated"
        )

    try:
        # Build the full prompt with context
        full_prompt = request.user_prompt
        if request.context:
            full_prompt = f"{request.user_prompt}\n\nContext:\n{request.context}"

        # Build options
        options = build_options(
            model=request.model,
            system_prompt=request.system_prompt
        )

        # Execute query
        response_text, metadata = await collect_response(
            query(prompt=full_prompt, options=options)
        )

        return {
            "response": response_text,
            "status": "success",
            "metadata": metadata
        }

    except Exception as e:
        logger.error(f"Error in structured_prompt endpoint: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Claude SDK error: {str(e)}"
        )


@app.post("/analyze/file", response_model=StructuredPromptResponse)
async def analyze_file(request: FileAnalysisRequest):
    """
    Analyze file content (HTML, JSON, XML, code, etc.)
    Specialized endpoint for content analysis
    """
    if auth_helper is None or not auth_helper.is_authenticated():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated"
        )

    try:
        # Build analysis prompt
        prompt = f"""Analyze this {request.content_type} content:

{request.analysis_instructions}

Content:
{request.content}"""

        # Build options
        options = build_options(model=request.model)

        # Execute query
        response_text, metadata = await collect_response(
            query(prompt=prompt, options=options)
        )

        return {
            "response": response_text,
            "status": "success",
            "metadata": metadata
        }

    except Exception as e:
        logger.error(f"Error in analyze_file endpoint: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Claude SDK error: {str(e)}"
        )


@app.post("/conversation", response_model=ChatResponse)
async def multi_turn_conversation(request: ConversationRequest):
    """
    Multi-turn conversation with context retention
    Uses ClaudeSDKClient to maintain conversation state
    """
    if auth_helper is None or not auth_helper.is_authenticated():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated"
        )

    try:
        # Build options
        options = build_options(
            model=request.model,
            system_prompt=request.system_prompt
        )

        # Use ClaudeSDKClient for conversation
        async with ClaudeSDKClient(options=options) as client:
            response_text = []
            metadata = {}

            # Send all messages in sequence
            for msg in request.messages:
                if msg.role == "user":
                    await client.query(msg.content)
                    # Collect response for this query
                    async for message in client.receive_response():
                        if isinstance(message, AssistantMessage):
                            for block in message.content:
                                if isinstance(block, TextBlock):
                                    response_text.append(block.text)
                            metadata["model"] = message.model
                        elif isinstance(message, ResultMessage):
                            metadata["duration_ms"] = message.duration_ms
                            metadata["num_turns"] = message.num_turns

            return {
                "response": "\n".join(response_text),
                "status": "success",
                "metadata": metadata
            }

    except Exception as e:
        logger.error(f"Error in conversation endpoint: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Claude SDK error: {str(e)}"
        )


@app.get("/version")
async def get_version():
    """Get Claude Code version"""
    try:
        import subprocess
        result = subprocess.run(
            ['claude', '--version'],
            capture_output=True,
            text=True,
            timeout=5
        )

        if result.returncode == 0:
            return {
                "version": result.stdout.strip(),
                "status": "success"
            }
        else:
            return {
                "version": "unknown",
                "status": "error",
                "error": result.stderr
            }
    except Exception as e:
        return {
            "version": "unknown",
            "status": "error",
            "error": str(e)
        }


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=os.getenv("LOG_LEVEL", "info").lower()
    )
