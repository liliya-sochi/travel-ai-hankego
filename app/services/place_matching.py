"""Детерминированное сопоставление названий туристических мест."""

import unicodedata


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
