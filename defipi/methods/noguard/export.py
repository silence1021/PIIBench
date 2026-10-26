"""Offline export of clean benchmark pages."""
def add_arguments(parser):
    pass

def export(pages, args):
    return {"pages": [{"page_id": p["page_id"]} for p in pages["pages"]],
            "files": {}, "defense_variant": "clean", "provenance": {}}
