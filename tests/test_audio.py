import wave

from faster_whisper.audio import decode_audio


def test_installed_decoder_reads_local_audio(tmp_path):
    """Catch PyAV API drift at the actual faster-whisper audio boundary."""
    path = tmp_path / 'silence.wav'
    with wave.open(str(path), 'wb') as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(b'\x00\x00' * 16000)
    samples = decode_audio(str(path))
    assert len(samples) == 16000
    assert abs(samples).max() == 0
