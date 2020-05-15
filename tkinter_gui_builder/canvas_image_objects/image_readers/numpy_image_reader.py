from abc import ABC

from tkinter_gui_builder.canvas_image_objects.image_readers.image_reader import AbstractImageReader
import numpy


class NumpyImageReader(AbstractImageReader, ABC):
    fname = None
    full_image_nx = int
    full_image_ny = int
    numpy_image_data = None     # type: numpy.ndarray

    def __init__(self,
                 numpy_image_data,          # type: numpy.ndarray
                 ):
        self.numpy_image_data = numpy_image_data
        self.full_image_ny, self.full_image_nx = numpy_image_data.shape

    def get_image_chip(self,
                       y_start,     # type: int
                       y_end,       # type: int
                       x_start,     # type: int
                       x_end,       # type: int
                       decimation,  # type: int
                       ):           # type: (...) -> numpy.ndarray
        if decimation < 1:
            decimation = 1
        rect_data = self.numpy_image_data[y_start:y_end:decimation, x_start:x_end:decimation]
        return rect_data
