"""Resolution and content checks for explicitly explained editorial crops."""
from pathlib import Path

from PIL import Image, ImageOps, ImageStat

from src.editorial_brief import EditorialNote, validate_editorial_brief
from src.editorial_copy import check_copy
from src.quality_filter import validate_local_image_file
from src.story_plan import rectangle_overlap, usable_detail


def validate_editorial_details(value, sources) -> tuple[EditorialNote, ...]:
    brief = validate_editorial_brief(value, sources)
    registry = {source.artwork.canonical_id: source for source in sources}
    details = []
    for note in brief.notes:
        if note.focus is None:
            continue
        if len(note.heading.strip()) < 3:
            raise ValueError('Each detail requires a specific heading and observation')
        check_copy(note.heading)
        if note.kind == 'visual_note':
            check_copy(note.text)
        if any(previous.artwork_id == note.artwork_id
               and rectangle_overlap(previous.focus, note.focus) >= 0.75 for previous in details):
            raise ValueError('Repeated crop does not add a distinct editorial observation')
        path = Path(registry[note.artwork_id].image_path)
        if path.stat().st_size > 30_000_000 or not validate_local_image_file(str(path)).valid:
            raise ValueError('Detail source is not a bounded validated image')
        with Image.open(path) as opened:
            image = ImageOps.exif_transpose(opened).convert('RGB')
        if not usable_detail(image.width, image.height, note.focus):
            raise ValueError('Detail resolution is insufficient')
        box = tuple(round(value * size) for value, size in zip(
            note.focus, (image.width, image.height, image.width, image.height)))
        crop = image.crop(box)
        crop.thumbnail((360, 360), Image.Resampling.LANCZOS)
        if ImageStat.Stat(ImageOps.grayscale(crop)).stddev[0] < 6:
            raise ValueError('Detail contains too little visual information')
        details.append(note)
    return tuple(details)
