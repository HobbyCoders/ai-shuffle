"""V2 client command models (discriminated union).

Single source of truth for the typed command channel. TS types are generated
from the JSON schema exported here — see app/contract/export.py.
"""

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


class CommandBase(BaseModel):
    command_id: str  # client-generated, echoed on resulting events
    session_id: str
    device_id: str | None = None


class SendMessage(CommandBase):
    type: Literal["send_message"] = "send_message"
    content: str
    attachments: list[str] = Field(default_factory=list)


class Interrupt(CommandBase):
    type: Literal["interrupt"] = "interrupt"
    mode: Literal["stop", "send_now"]


class QueueMessage(CommandBase):
    type: Literal["queue_message"] = "queue_message"
    content: str


class RemoveQueued(CommandBase):
    type: Literal["remove_queued"] = "remove_queued"
    queued_command_id: str


class AnswerInteraction(CommandBase):
    type: Literal["answer_interaction"] = "answer_interaction"
    interaction_id: str
    answer: dict = Field(default_factory=dict)


Command = Annotated[
    Union[
        SendMessage,
        Interrupt,
        QueueMessage,
        RemoveQueued,
        AnswerInteraction,
    ],
    Field(discriminator="type"),
]

COMMAND_TYPES: tuple[str, ...] = (
    "send_message",
    "interrupt",
    "queue_message",
    "remove_queued",
    "answer_interaction",
)
