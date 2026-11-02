"""Small, strict parser for systemd's whitespace-delimited setting values."""


def words(value: str) -> list[str]:
    """Return decoded systemd words, raising ValueError for malformed syntax."""
    import os

    escapes = {
        "a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r",
        "t": "\t", "v": "\v", "\\": "\\", '"': '"', "'": "'", "s": " ",
    }

    def escape(index: int) -> tuple[str, int]:
        if index + 1 >= len(value):
            raise ValueError("trailing backslash escape")
        code = value[index + 1]
        if code in escapes:
            return escapes[code], index + 2
        if code in "xuU":
            width = {"x": 2, "u": 4, "U": 8}[code]
            digits = value[index + 2:index + 2 + width]
            if len(digits) != width or any(
                    char not in "0123456789abcdefABCDEF" for char in digits):
                raise ValueError(f"invalid escape \\{code}{digits}")
            number = int(digits, 16)
            if number == 0:
                raise ValueError("NUL is not allowed")
            if number > 0x10ffff or 0xd800 <= number <= 0xdfff:
                raise ValueError(f"invalid Unicode escape \\{code}{digits}")
            decoded = os.fsdecode(bytes([number])) if code == "x" else chr(number)
            return decoded, index + 2 + width
        if code in "01234567":
            digits = value[index + 1:index + 4]
            if len(digits) != 3 or any(char not in "01234567" for char in digits):
                raise ValueError(f"invalid octal escape \\{digits}")
            number = int(digits, 8)
            if number == 0:
                raise ValueError("NUL is not allowed")
            if number > 255:
                raise ValueError(f"invalid octal escape \\{digits}")
            return os.fsdecode(bytes([number])), index + 4
        raise ValueError(f"invalid escape \\{code}")

    if "\0" in value:
        raise ValueError("NUL is not allowed")
    result: list[str] = []
    token: list[str] = []
    started = False
    quote: str | None = None
    index = 0
    while index < len(value):
        char = value[index]
        if quote is not None:
            if char == quote:
                if index + 1 < len(value) and not value[index + 1].isspace():
                    raise ValueError("closing quote must be followed by whitespace")
                quote = None
                index += 1
            elif char == "\\":
                decoded, index = escape(index)
                token.append(decoded)
            else:
                token.append(char)
                index += 1
            continue
        if char.isspace():
            if started:
                result.append(os.fsdecode(os.fsencode("".join(token))))
                token = []
                started = False
            index += 1
        elif char in "\"'":
            if started:
                raise ValueError("opening quote must follow whitespace")
            quote = char
            started = True
            index += 1
        elif char == "\\":
            started = True
            decoded, index = escape(index)
            token.append(decoded)
        else:
            started = True
            token.append(char)
            index += 1
    if quote is not None:
        name = "double" if quote == '"' else "single"
        raise ValueError(f"unterminated {name} quote")
    if started:
        result.append(os.fsdecode(os.fsencode("".join(token))))
    return result
