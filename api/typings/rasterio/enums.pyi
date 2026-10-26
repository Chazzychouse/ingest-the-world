from enum import IntEnum

class Resampling(IntEnum):
    nearest = 0
    average = 5

class ColorInterp(IntEnum):
    undefined = 0
    gray = 1
    palette = 2
    red = 3
    green = 4
    blue = 5
    alpha = 6
