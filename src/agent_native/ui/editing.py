"""Pure finite single-line edit operations over canonical C1 Draft values.

Offsets use UTF-16 units. Unsupported shortcuts/IME attributes are refused by
the native adapter. No Qt event/widget or alternative store is retained here.
"""
def units(text):
    return len(text.encode("utf-16-le")) // 2


def boundaries(text):
    result = [0]
    for char in text:
        result.append(result[-1] + units(char))
    return result


def position(text, offset):
    offsets = boundaries(text)
    if type(offset) is not int or offset not in offsets:
        raise ValueError("Invalid UTF-16 boundary")
    return offsets.index(offset)


def validated(values):
    result = dict(values)
    for name in ("cursor_utf16", "anchor_utf16"):
        position(result["text"], result[name])
    for name in ("preedit_cursor_utf16", "preedit_anchor_utf16"):
        position(result["preedit"], result[name])
    position(result["text"],result["replacement_start_utf16"])
    position(result["text"],result["replacement_start_utf16"]+result["replacement_length_utf16"])
    return result


def replace(values, start, end, value):
    text = values["text"]
    a, b = position(text, start), position(text, end)
    if any(char in value for char in ("\r", "\n", "\x00")):
        raise ValueError("Single-line input required")
    result = dict(values)
    result.update(text=text[:a] + value + text[b:], cursor_utf16=start + units(value),
                  anchor_utf16=start + units(value), preedit="", composing=False,
                  preedit_cursor_utf16=0, preedit_anchor_utf16=0,
                  replacement_start_utf16=start, replacement_length_utf16=0)
    return result


def key_edit(values, key, text="", *, shift=False, select_all=False):
    values = validated(values)
    cursor, anchor = values["cursor_utf16"], values["anchor_utf16"]
    offsets = boundaries(values["text"])
    lo, hi = sorted((cursor, anchor))
    if select_all:
        return {**values, "anchor_utf16": 0, "cursor_utf16": offsets[-1]}
    if key in ("left", "right", "home", "end"):
        if key == "home":
            target = 0
        elif key == "end":
            target = offsets[-1]
        elif lo != hi and not shift:
            target = lo if key == "left" else hi
        else:
            index = offsets.index(cursor)
            target = offsets[max(0, index - 1)] if key == "left" else offsets[min(len(offsets)-1, index+1)]
        return {**values, "cursor_utf16": target, "anchor_utf16": anchor if shift else target,
                "preedit": "", "composing": False, "preedit_cursor_utf16": 0, "preedit_anchor_utf16": 0}
    if key in ("backspace", "delete"):
        if lo == hi:
            index = offsets.index(cursor)
            lo = offsets[max(0, index-1)] if key == "backspace" else cursor
            hi = cursor if key == "backspace" else offsets[min(len(offsets)-1, index+1)]
        return replace(values, lo, hi, "")
    if key == "text" and text and text.isprintable():
        return replace(values, lo, hi, text)
    raise ValueError("Unsupported editor key")


def ime_edit(values, commit, preedit, relative_start, length, cursor=None):
    values = validated(values)
    if type(relative_start) is not int or type(length) is not int or length < 0:
        raise ValueError("Invalid IME replacement")
    lo, hi = sorted((values["cursor_utf16"], values["anchor_utf16"]))
    # Qt commits remove the current selection before relative replacement.
    base = replace(values, lo, hi, "") if commit or length else dict(values)
    start = base["cursor_utf16"] + relative_start
    end = start + length
    position(base["text"], start)
    position(base["text"], end)
    result = replace(base, start, end, commit) if commit or length else base
    caret = units(preedit) if cursor is None else cursor
    position(preedit, caret)
    result.update(preedit=preedit, composing=bool(preedit),
                  preedit_cursor_utf16=caret, preedit_anchor_utf16=caret,
                  replacement_start_utf16=start,
                  # The replacement was consumed; canonical range is in CURRENT text.
                  replacement_length_utf16=units(commit))
    return result
