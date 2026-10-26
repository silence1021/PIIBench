#!/usr/bin/env python3


def normalize_instruction(text):
    return " ".join(str(text or "").split()).strip()
