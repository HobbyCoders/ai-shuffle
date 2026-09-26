"""
Tests for the generated_audio API endpoints (thin shim over media_serve).

Audio had no coverage before the media-serve consolidation. These tests
assert the by-path route serves a temp file, rejects path traversal / files
outside the generated-audio directory, and 404s missing files, plus the
audio-specific MIME table and default fallback.
"""

import pytest
from unittest.mock import patch


class TestAudioConstants:
    def test_allowed_extensions(self):
        from app.api.generated_audio import ALLOWED_EXTENSIONS

        for ext in ('.mp3', '.wav', '.ogg', '.oga', '.opus', '.flac', '.m4a', '.aac', '.pcm'):
            assert ext in ALLOWED_EXTENSIONS

    def test_media_types(self):
        from app.api.generated_audio import MEDIA_TYPES

        assert MEDIA_TYPES['.mp3'] == 'audio/mpeg'
        assert MEDIA_TYPES['.wav'] == 'audio/wav'
        assert MEDIA_TYPES['.pcm'] == 'audio/L16'

    def test_all_extensions_have_media_types(self):
        from app.api.generated_audio import ALLOWED_EXTENSIONS, MEDIA_TYPES

        for ext in ALLOWED_EXTENSIONS:
            assert ext in MEDIA_TYPES

    def test_router_prefix_and_tag(self):
        from app.api.generated_audio import router

        assert router.prefix == "/api/generated-audio"
        assert "generated-audio" in router.tags

    def test_is_path_safe_reexported(self):
        from app.api.generated_audio import is_path_safe

        assert callable(is_path_safe)


class TestGetAudioByPath:
    @pytest.mark.asyncio
    async def test_serves_temp_file(self, temp_dir):
        from app.api.generated_audio import get_audio_by_path
        from fastapi.responses import FileResponse

        gen_dir = temp_dir / "generated-audio"
        gen_dir.mkdir()
        test_file = gen_dir / "clip.mp3"
        test_file.write_bytes(b"ID3" + b"\x00" * 100)

        with patch("app.api.generated_audio.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = await get_audio_by_path(path=str(test_file))
            assert isinstance(response, FileResponse)
            assert response.media_type == "audio/mpeg"
            assert response.filename == "clip.mp3"

    @pytest.mark.asyncio
    async def test_wav_mime_type(self, temp_dir):
        from app.api.generated_audio import get_audio_by_path

        gen_dir = temp_dir / "generated-audio"
        gen_dir.mkdir()
        test_file = gen_dir / "clip.wav"
        test_file.write_bytes(b"RIFF" + b"\x00" * 100)

        with patch("app.api.generated_audio.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = await get_audio_by_path(path=str(test_file))
            assert response.media_type == "audio/wav"

    @pytest.mark.asyncio
    async def test_invalid_extension(self, temp_dir):
        from app.api.generated_audio import get_audio_by_path
        from fastapi import HTTPException

        gen_dir = temp_dir / "generated-audio"
        gen_dir.mkdir()
        test_file = gen_dir / "notes.txt"
        test_file.write_text("nope")

        with patch("app.api.generated_audio.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            with pytest.raises(HTTPException) as exc:
                await get_audio_by_path(path=str(test_file))
            assert exc.value.status_code == 400
            assert "Invalid file type" in exc.value.detail

    @pytest.mark.asyncio
    async def test_outside_workspace(self, temp_dir):
        from app.api.generated_audio import get_audio_by_path
        from fastapi import HTTPException

        with patch("app.api.generated_audio.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            with pytest.raises(HTTPException) as exc:
                await get_audio_by_path(path="/etc/passwd.mp3")
            assert exc.value.status_code == 403
            assert "outside workspace" in exc.value.detail.lower()

    @pytest.mark.asyncio
    async def test_path_traversal(self, temp_dir):
        from app.api.generated_audio import get_audio_by_path
        from fastapi import HTTPException

        gen_dir = temp_dir / "generated-audio"
        gen_dir.mkdir()

        with patch("app.api.generated_audio.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            with pytest.raises(HTTPException) as exc:
                await get_audio_by_path(path=str(temp_dir / ".." / ".." / "etc" / "x.mp3"))
            assert exc.value.status_code == 403
            assert "outside workspace" in exc.value.detail.lower()

    @pytest.mark.asyncio
    async def test_not_in_generated_audio_dir(self, temp_dir):
        from app.api.generated_audio import get_audio_by_path
        from fastapi import HTTPException

        test_file = temp_dir / "clip.mp3"
        test_file.write_bytes(b"ID3" + b"\x00" * 100)

        with patch("app.api.generated_audio.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            with pytest.raises(HTTPException) as exc:
                await get_audio_by_path(path=str(test_file))
            assert exc.value.status_code == 403
            # preserves the audio-specific wording "not a generated audio file"
            assert "not a generated audio file" in exc.value.detail.lower()

    @pytest.mark.asyncio
    async def test_not_found(self, temp_dir):
        from app.api.generated_audio import get_audio_by_path
        from fastapi import HTTPException

        gen_dir = temp_dir / "generated-audio"
        gen_dir.mkdir()

        with patch("app.api.generated_audio.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            with pytest.raises(HTTPException) as exc:
                await get_audio_by_path(path=str(gen_dir / "missing.mp3"))
            assert exc.value.status_code == 404
            assert "not found" in exc.value.detail.lower()


class TestGetAudioByFilename:
    @pytest.mark.asyncio
    async def test_serves_file(self, temp_dir):
        from app.api.generated_audio import get_generated_audio
        from fastapi.responses import FileResponse

        gen_dir = temp_dir / "generated-audio"
        gen_dir.mkdir()
        (gen_dir / "voice.wav").write_bytes(b"RIFF" + b"\x00" * 100)

        with patch("app.api.generated_audio.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = await get_generated_audio(filename="voice.wav")
            assert isinstance(response, FileResponse)
            assert response.media_type == "audio/wav"

    @pytest.mark.asyncio
    async def test_traversal_forward_slash(self, temp_dir):
        from app.api.generated_audio import get_generated_audio
        from fastapi import HTTPException

        gen_dir = temp_dir / "generated-audio"
        gen_dir.mkdir()

        with patch("app.api.generated_audio.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            with pytest.raises(HTTPException) as exc:
                await get_generated_audio(filename="sub/clip.mp3")
            assert exc.value.status_code == 400
            assert "Invalid filename" in exc.value.detail

    @pytest.mark.asyncio
    async def test_missing_file_404(self, temp_dir):
        from app.api.generated_audio import get_generated_audio
        from fastapi import HTTPException

        gen_dir = temp_dir / "generated-audio"
        gen_dir.mkdir()

        with patch("app.api.generated_audio.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            with pytest.raises(HTTPException) as exc:
                await get_generated_audio(filename="nope.mp3")
            assert exc.value.status_code == 404


class TestListGeneratedAudio:
    @pytest.mark.asyncio
    async def test_empty_when_missing_dir(self, temp_dir):
        from app.api.generated_audio import list_generated_audio

        with patch("app.api.generated_audio.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            result = await list_generated_audio()
            assert result == {"audio": []}

    @pytest.mark.asyncio
    async def test_lists_files_with_audio_key_and_urls(self, temp_dir):
        from app.api.generated_audio import list_generated_audio

        gen_dir = temp_dir / "generated-audio"
        gen_dir.mkdir()
        (gen_dir / "a.mp3").write_bytes(b"\x00" * 10)

        with patch("app.api.generated_audio.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            result = await list_generated_audio()
            assert "audio" in result
            assert len(result["audio"]) == 1
            assert result["audio"][0]["url"] == "/api/generated-audio/a.mp3"
