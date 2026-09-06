import re
import pytest

def normalize_question(q: str) -> str:
    cleaned = re.sub(r"[^\w\s]", "", q.lower())
    return " ".join(cleaned.split())


def test_normalize_question_basics():
    assert normalize_question("What is your favorite book?") == "what is your favorite book"
    assert normalize_question("  WHAT   IS   YOUR   FAVORITE   BOOK?!  ") == "what is your favorite book"
    assert normalize_question("Jaká je tvá oblíbená kniha?") == "jaká je tvá oblíbená kniha"


def test_duplicate_identification():
    existing = ["What is your favorite book?", "Jaká je tvá oblíbená kniha?"]
    existing_norms = {normalize_question(q) for q in existing}

    candidates = [
        "what is your favorite book",           # Duplicate (diff punctuation/case)
        "What is your favorite book!?",        # Duplicate (diff punctuation)
        "what is your favorite movie?",         # Unique
        "Jaká je tvá oblíbená kniha",           # Duplicate
        "Jaký je tvůj oblíbený film?",          # Unique
    ]

    unique = []
    duplicates = []
    for c in candidates:
        norm = normalize_question(c)
        if norm in existing_norms:
            duplicates.append(c)
        else:
            existing_norms.add(norm)
            unique.append(c)

    assert len(unique) == 2
    assert "what is your favorite movie?" in unique
    assert "Jaký je tvůj oblíbený film?" in unique
    assert len(duplicates) == 3
