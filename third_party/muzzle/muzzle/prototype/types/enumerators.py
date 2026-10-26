from enum import Enum

class TranscriptType(str, Enum):
    ADVERSARIAL = "adversarial"
    BENIGN = "benign"