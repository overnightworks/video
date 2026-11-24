"""Read a configuration file into a validated document without echoing its values."""

from __future__ import annotations

import tomllib
from typing import TYPE_CHECKING

from pydantic import ValidationError

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import TypeAdapter


def read_toml_file[Document](
    path: Path, document_form: TypeAdapter[Document], *, variable: str, form: str
) -> Document:
    """The file's document in `document_form`; a fault names a field, never a value.

    `variable` is the setting that names the file and `form` what the file must be.
    """
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError, UnicodeDecodeError, tomllib.TOMLDecodeError:
        message = f"{variable} must name a readable TOML file"
        raise ValueError(message) from None
    try:
        return document_form.validate_python(document)
    except ValidationError as error:
        faults = "; ".join(
            f"{'.'.join(map(str, fault['loc']))}: {fault['msg']}"
            if fault["loc"]
            else fault["msg"]
            for fault in error.errors(include_input=False, include_url=False)
        )
        message = f"{variable} breaks {form} ({faults})"
        raise ValueError(message) from None
