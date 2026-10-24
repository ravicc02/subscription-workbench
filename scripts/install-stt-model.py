"""Install the pinned CPU speech model into the ignored local state directory."""

from pathlib import Path

from huggingface_hub import snapshot_download

REPO = "Systran/faster-whisper-tiny"
REVISION = "d90ca5fe260221311c53c58e660288d3deb8d356"
FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")


def main():
    destination = Path(__file__).resolve().parents[1] / "state/models/faster-whisper-tiny"
    snapshot_download(
        repo_id=REPO, revision=REVISION, local_dir=destination, allow_patterns=list(FILES)
    )
    if not all((destination / name).is_file() for name in FILES):
        raise RuntimeError("Speech model download is incomplete")
    print(destination)


if __name__ == "__main__":
    main()
