"""Runs XCoder tools on uploaded files inside a throwaway work directory."""

import os
import shutil
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from loguru import logger

from xcoder.features.csv.compress import compress_csv
from xcoder.features.csv.decompress import decompress_csv
from xcoder.features.sc.decode import decode_and_render_objects, decode_textures_only
from xcoder.features.sc.encode import collect_objects_and_encode, encode_textures_only

MAX_UNZIPPED_BYTES = int(os.environ.get("MAX_UNZIPPED_MB", "600")) * 1024 * 1024


class JobError(Exception):
    pass


@dataclass
class Tool:
    id: str
    label: str
    help: str
    run: Callable[[Path, Path], None]
    folder_input: bool  # True: expects a decoded folder (.xcod + images)


TOOLS: list[Tool] = [
    Tool(
        "sc_decode",
        "Decode SC (textures)",
        "Upload the *_tex.sc file(s). You get PNG textures plus an .xcod file.",
        lambda i, o: decode_textures_only(i, o),
        False,
    ),
    Tool(
        "sc_encode",
        "Encode SC (textures)",
        "Upload the zip you got from Decode (or the .xcod and PNG files together).",
        lambda i, o: encode_textures_only(i, o, resize=True),
        True,
    ),
    Tool(
        "sc_decode_parts",
        "Decode SC (by parts)",
        "Upload BOTH the .sc and the matching _tex.sc. You get sprites, shapes and textures.",
        lambda i, o: decode_and_render_objects(i, o),
        False,
    ),
    Tool(
        "sc_encode_parts",
        "Encode SC (by parts)",
        "Upload the zip you got from Decode (by parts), after editing the sprites.",
        lambda i, o: collect_objects_and_encode(False, i, o, True),
        True,
    ),
    Tool(
        "sc_overwrite_parts",
        "Overwrite SC (by parts)",
        "Same as Encode by parts, but only applies the sprites in the 'overwrite' folder.",
        lambda i, o: collect_objects_and_encode(True, i, o, True),
        True,
    ),
    Tool(
        "csv_decompress",
        "Decompress CSV",
        "Upload compressed .csv file(s).",
        lambda i, o: decompress_csv(str(i), str(o)),
        False,
    ),
    Tool(
        "csv_compress",
        "Compress CSV",
        "Upload decompressed .csv file(s).",
        lambda i, o: compress_csv(str(i), str(o)),
        False,
    ),
]
TOOLS_BY_ID = {t.id: t for t in TOOLS}


def _safe_extract(zip_path: Path, dest: Path) -> None:
    dest_resolved = dest.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        members = [
            m
            for m in archive.infolist()
            if not m.is_dir()
            and "__MACOSX" not in m.filename
            and not Path(m.filename).name.startswith(".")
        ]
        if sum(m.file_size for m in members) > MAX_UNZIPPED_BYTES:
            raise JobError("Zip is too large once extracted.")
        for member in members:
            target = (dest / member.filename).resolve()
            if not str(target).startswith(str(dest_resolved) + os.sep):
                raise JobError("Zip contains an unsafe path.")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)


def _stage(raw_dir: Path, in_dir: Path, folder_input: bool) -> None:
    for file in sorted(raw_dir.iterdir()):
        if file.suffix.lower() == ".zip":
            _safe_extract(file, in_dir)
        else:
            shutil.copy(file, in_dir / file.name)

    if folder_input:
        # Loose .xcod at the top level means the contents were zipped without
        # their parent folder: wrap them in a folder named after the .xcod.
        xcods = list(in_dir.glob("*.xcod"))
        if xcods:
            wrapper = in_dir / xcods[0].stem
            wrapper.mkdir(exist_ok=True)
            for item in list(in_dir.iterdir()):
                if item != wrapper:
                    shutil.move(str(item), wrapper / item.name)
    else:
        # Flat tools: pull files out of any sub folders.
        for file in list(in_dir.rglob("*")):
            if file.is_file() and file.parent != in_dir:
                shutil.move(str(file), in_dir / file.name)


def run_job(tool_id: str, raw_dir: Path, work_dir: Path) -> tuple[Path, str]:
    """Returns (path to result zip, log text). Raises JobError if nothing came out."""
    tool = TOOLS_BY_ID[tool_id]
    in_dir, out_dir = work_dir / "in", work_dir / "out"
    in_dir.mkdir()
    out_dir.mkdir()

    lines: list[str] = []
    thread_id = threading.get_ident()
    sink = logger.add(
        lambda message: lines.append(str(message).rstrip()),
        format="[{level}] {message}",
        level="INFO",
        filter=lambda record: record["thread"].id == thread_id,
    )
    try:
        _stage(raw_dir, in_dir, tool.folder_input)
        if tool_id == "sc_decode" and not any(in_dir.glob("*_tex.sc")):
            raise JobError(
                "No *_tex.sc file found. Upload the file ending in _tex.sc, "
                "or use Decode SC (by parts) with the .sc file."
            )
        tool.run(in_dir, out_dir)
    except JobError:
        raise
    except SystemExit:
        lines.append("[ERROR] The file could not be decompressed.")
    except Exception as exception:  # noqa: BLE001
        lines.append(f"[ERROR] {type(exception).__name__}: {exception}")
    finally:
        logger.remove(sink)

    log_text = "\n".join(line for line in lines if line.strip())
    outputs = [f for f in out_dir.rglob("*") if f.is_file()]
    if not outputs:
        raise JobError(log_text or "Nothing was produced. Check the files you uploaded.")

    (out_dir / "log.txt").write_text(log_text, encoding="utf-8")
    zip_path = work_dir / "result.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in out_dir.rglob("*"):
            if file.is_file():
                archive.write(file, file.relative_to(out_dir))
    return zip_path, log_text
