"""Детерминированное сопоставление названий туристических мест."""

import unicodedata


def saved_place_name_is_explicit(*, instruction: str, saved_name: str) -> bool:
    """Ищет полное сохранённое название или его известный вариант в скобках."""

    display_name = unicodedata.normalize("NFKC", saved_name).strip()
    aliases = [display_name]
    if display_name.endswith(")"):
        depth = 0
        for index in range(len(display_name) - 1, -1, -1):
            character = display_name[index]
            if character == ")":
                depth += 1
            elif character == "(":
                depth -= 1
                if depth == 0:
                    prefix = display_name[:index].strip()
                    if prefix:
                        aliases.extend((prefix, display_name[index + 1 : -1]))
                    break
    normalized_instruction = normalize_place_name(instruction)
    return any(
        len(alias) >= 4 and alias in normalized_instruction
        for alias in map(normalize_place_name, aliases)
    )


def normalize_place_name(value: str) -> str:
    """Убирает регистр, диакритику, пробелы и знаки препинания."""

    decomposed = unicodedata.normalize("NFKD", value.casefold())

    return "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character) and character.isalnum()
    )


def required_place_name_matches(
    *,
    required_name: str,
    candidate_name: str,
) -> bool:
    """Сопоставляет точное название и уточнение внутри скобок."""

    normalized_required = normalize_place_name(required_name)
    normalized_candidate = normalize_place_name(candidate_name)

    if not normalized_required or not normalized_candidate:
        return False

    if normalized_required == normalized_candidate:
        return True

    if len(normalized_required) <= len(normalized_candidate):
        shorter_name, longer_name = normalized_required, normalized_candidate
    else:
        shorter_name, longer_name = normalized_candidate, normalized_required

    return len(shorter_name) >= 4 and shorter_name in longer_name
