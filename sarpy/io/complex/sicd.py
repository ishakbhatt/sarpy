# -*- coding: utf-8 -*-
"""
Module for reading SICD files - should support SICD version 0.3 and above.
"""

import re
import sys
import logging

import numpy

from .base import validate_sicd_for_writing, string_types
from .nitf import MultiSegmentChipper, NITFReader, NITFWriter, ImageDetails, DESDetails, \
    image_segmentation, get_npp_block, interpolate_corner_points_string
from .utils import parse_xml_from_string
# noinspection PyProtectedMember
from .sicd_elements.SICD import SICDType, _SICD_SPECIFICATION_IDENTIFIER

from ..nitf.nitf_head import NITFDetails
from ..nitf.des import DataExtensionHeader, SICDDESSubheader
from ..nitf.security import NITFSecurityTags
from ..nitf.image import ImageSegmentHeader, ImageBands, ImageBand


if sys.version_info[0] < 3:
    # noinspection PyUnresolvedReferences
    from cStringIO import StringIO
    NOT_FOUND_ERROR = IOError
else:
    from io import StringIO
    # noinspection PyUnresolvedReferences
    NOT_FOUND_ERROR = FileNotFoundError

__classification__ = "UNCLASSIFIED"
__author__ = ("Thomas McCullough", "Wade Schwartzkopf")


########
# base expected functionality for a module with an implemented Reader

def is_a(file_name):
    """
    Tests whether a given file_name corresponds to a SICD file. Returns a reader instance, if so.

    Parameters
    ----------
    file_name : str
        the file_name to check

    Returns
    -------
    SICDReader|None
        `SICDReader` instance if SICD file, `None` otherwise
    """

    try:
        nitf_details = SICDDetails(file_name)
        if nitf_details.is_sicd:
            print('File {} is determined to be a SICD (NITF format) file.'.format(file_name))
            return SICDReader(nitf_details)
        else:
            return None
    except IOError:
        # we don't want to catch parsing errors, for now
        return None


#########
# Helper object for initially parses NITF header - specifically looking for SICD elements


class SICDDetails(NITFDetails):
    """
    SICD are stored in NITF 2.1 files.
    """
    __slots__ = (
        '_des_index', '_des_header', '_img_headers',
        '_is_sicd', '_sicd_meta', 'img_segment_rows', 'img_segment_columns')

    def __init__(self, file_name):
        """

        Parameters
        ----------
        file_name : str
            file name for a NITF 2.1 file containing a SICD
        """

        self._des_index = None
        self._des_header = None
        self._img_headers = None
        self._is_sicd = False
        self._sicd_meta = None
        super(SICDDetails, self).__init__(file_name)
        if self._nitf_header.ImageSegments.subhead_sizes.size == 0:
            raise IOError('There are no image segments defined.')
        if self._nitf_header.GraphicsSegments.item_sizes.size > 0:
            raise IOError('A SICD file does not allow for graphics segments.')
        if self._nitf_header.DataExtensions.subhead_sizes.size == 0:
            raise IOError('A SICD file requires at least one data extension, containing the '
                          'SICD xml structure.')
        # define the sicd metadata
        self._find_sicd()
        # populate the image details
        self.img_segment_rows = numpy.zeros(self.img_segment_offsets.shape, dtype=numpy.int64)
        self.img_segment_columns = numpy.zeros(self.img_segment_offsets.shape, dtype=numpy.int64)
        for i, im_header in enumerate(self.img_headers):
            self.img_segment_rows[i] = im_header.NROWS
            self.img_segment_columns[i] = im_header.NCOLS

    @property
    def is_sicd(self):
        """
        bool: whether file name corresponds to a SICD file, or not.
        """

        return self._is_sicd

    @property
    def sicd_meta(self):
        """
        sarpy.io.complex.sicd_elements.SICD.SICDType: the sicd meta-data structure.
        """

        return self._sicd_meta

    @property
    def img_headers(self):
        """
        The image segment headers.

        Returns
        -------
            None|List[sarpy.io.nitf.image.ImageSegmentHeader]
        """

        if self._img_headers is not None:
            return self._img_headers

        self._parse_img_headers()
        return self._img_headers

    @property
    def des_header(self):
        """
        The DES subheader object associated with the SICD.

        Returns
        -------
        None|sarpy.io.nitf.des.DataExtensionHeader
        """

        return self._des_header

    def _parse_img_headers(self):
        if self.img_segment_offsets is None or self._img_headers is not None:
            return

        self._img_headers = [self.parse_image_subheader(i) for i in range(self.img_subheader_offsets.size)]

    def _find_sicd(self):
        self._is_sicd = False
        self._sicd_meta = None
        if self.des_subheader_offsets is None:
            return

        for i in range(self.des_subheader_offsets.size):
            subhead_bytes = self.get_des_subheader_bytes(i)
            if subhead_bytes.startswith(b'DEXML_DATA_CONTENT'):
                des_header = DataExtensionHeader.from_bytes(subhead_bytes, start=0)
                des_bytes = self.get_des_bytes(i)
                try:
                    root_node, xml_ns = parse_xml_from_string(des_bytes.decode('utf-8').strip())
                    if 'SIDD' in root_node.tag:  # namespace makes this ugly
                        # NOTE that SIDD files are supposed to have the corresponding
                        # SICD xml as one of the DES AFTER the SIDD xml.
                        # The same basic format is used for both headers.
                        # So, abandon if we find a SIDD xml
                        self._des_index = None
                        self._des_header = None
                        self._is_sicd = False
                        break
                    elif 'SICD' in root_node.tag:  # namespace makes this ugly
                        self._des_index = i
                        self._des_header = des_header
                        self._is_sicd = True
                        self._sicd_meta = SICDType.from_node(root_node, xml_ns, ns_key='default')
                        break
                except Exception:
                    continue
            elif subhead_bytes.startswith(b'DESIDD_XML'):
                # This is an old format SIDD and can't be a SICD
                self._des_index = None
                self._des_header = None
                self._is_sicd = False
                break
            elif subhead_bytes.startswith(b'DESICD_XML'):
                # This is an old format SICD
                des_bytes = self.get_des_bytes(i)
                try:
                    root_node, xml_ns = parse_xml_from_string(des_bytes)
                    if 'SICD' in root_node.tag:  # namespace makes this ugly
                        self._des_index = i
                        self._des_header = None
                        self._is_sicd = True
                        self._sicd_meta = SICDType.from_node(root_node, xml_ns, ns_key='default')
                        break
                except Exception as e:
                    logging.error('We found an apparent old-style SICD DES header, '
                                  'but failed parsing with error {}'.format(e))
                    continue

        if not self._is_sicd:
            return
        self._sicd_meta.derive()
        # TODO: account for the reference frequency offset situation

    def is_des_well_formed(self):
        """
        Returns whether the data extension subheader well-formed. Returns `None`
        if the DataExtensionHeader or the UserHeader section of it was not successfully
        parsed. Currently just checks the `DESSHSI` field for the required value.

        Returns
        -------
        bool|None
        """

        if not self._is_sicd or self.des_header is None or \
                not isinstance(self.des_header, DataExtensionHeader):
            return None

        sicd_des = self._des_header.UserHeader
        if not isinstance(sicd_des, SICDDESSubheader):
            return None
        return sicd_des.DESSHSI.strip() == _SICD_SPECIFICATION_IDENTIFIER

    def repair_des_header(self):
        """
        Determines whether the data extension subheader is well-formed, and tries
        to repair it if not. Currently just sets the `DESSHSI` field to the
        required value.

        Returns `0` if wellformedness could not be evaluated, `1` if no change was
        required, `2` if the subheader was replaced, and `3` if the replacement effort
        failed (details logged at error level).

        Returns
        -------
        int
        """

        stat = self.is_des_well_formed()
        if stat is None:
            return 0
        elif stat:
            return 1

        sicd_des = self._des_header.UserHeader
        # noinspection PyProtectedMember
        sicd_des.DESSHSI = _SICD_SPECIFICATION_IDENTIFIER
        stat = self.rewrite_des_header()
        return 2 if stat else 3

    def rewrite_des_header(self):
        """
        Rewrites the DES subheader associated with the SICD from the current
        value in `des_header` property. This allows minor modifications to the
        security tags or user header information.

        Returns
        -------
        bool
            True is the modification was successful and False otherwise. Note that
            no errors, in particular io errors from write permission issues,
            are caught.
        """

        if not self._is_sicd:
            return False

        des_bytes = self.des_header.to_bytes()
        des_size = self._nitf_header.DataExtensions.subhead_sizes[self._des_index]
        if len(des_bytes) != des_size:
            logging.error(
                "The size of the current des header {} bytes, does not match the "
                "previous {} bytes. They cannot be trivially replaced.".format(des_bytes, des_size))
            return False
        des_loc = self.des_subheader_offsets[self._des_index]
        with open(self._file_name, 'r+b') as fi:
            fi.seek(des_loc)
            fi.write(des_bytes)
        return True


#######
#  The actual reading implementation

def _validate_lookup(lookup_table):  # type: (numpy.ndarray) -> None
    if not isinstance(lookup_table, numpy.ndarray):
        raise ValueError('requires a numpy.ndarray, got {}'.format(type(lookup_table)))
    if lookup_table.dtype.name != 'float64':
        raise ValueError('requires a numpy.ndarray of float64 dtype, got {}'.format(lookup_table.dtype))
    if lookup_table.shape != (256, ):
        raise ValueError('Requires a one-dimensional numpy.ndarray with 256 elements, '
                         'got shape {}'.format(lookup_table.shape))


def amp_phase_to_complex(lookup_table):
    """
    This constructs the function to convert from AMP8I_PHS8I format data to complex64 data.

    Parameters
    ----------
    lookup_table : numpy.ndarray

    Returns
    -------
    callable
    """

    _validate_lookup(lookup_table)

    def converter(data):
        if not isinstance(data, numpy.ndarray):
            raise ValueError('requires a numpy.ndarray, got {}'.format(type(data)))

        if data.dtype.name != 'uint8':
            raise ValueError('requires a numpy.ndarray of uint8 dtype, got {}'.format(data.dtype.name))

        if len(data.shape) == 3:
            raise ValueError('Requires a three-dimensional numpy.ndarray (with band '
                             'in the last dimension), got shape {}'.format(data.shape))

        out = numpy.zeros((data.shape[0], data.shape[1], data.shape[2]/2), dtype=numpy.complex64)
        amp = lookup_table[data[:, :, 0::2]]
        theta = data[:, :, 1::2]*(2*numpy.pi/256)
        out.real = amp*numpy.cos(theta)
        out.imag = amp*numpy.sin(theta)
        return out
    return converter


class SICDReader(NITFReader):
    """
    A reader object for a SICD file (NITF container with SICD contents)
    """

    def __init__(self, nitf_details):
        """

        Parameters
        ----------
        nitf_details : str|sarpy.io.complex.sicd_elements.SICD.SICDDetails
            filename or SICDDetails object
        """

        if isinstance(nitf_details, string_types):
            nitf_details = SICDDetails(nitf_details)
        if not isinstance(nitf_details, SICDDetails):
            raise TypeError('The input argument for SICDReader must be a filename or '
                            'SICDDetails object.')
        if not nitf_details.is_sicd:
            raise ValueError(
                'The input file passed in appears to be a NITF 2.1 file that does '
                'not contain valid sicd metadata.')
        super(SICDReader, self).__init__(nitf_details)

        # to perform a preliminary check that the structure is valid:
        #   note that this results in potentially noisy logging for troubled sicd files
        self._sicd_meta.is_valid(recursive=True)

    def _find_segments(self):
        return list(range(self._nitf_details.img_segment_offsets.size))

    def _construct_chipper(self, segment, index):
        meta = self._sicd_meta
        pixel_type = meta.ImageData.PixelType
        # NB: SICDs are required to be stored as big-endian
        if pixel_type == 'RE32F_IM32F':
            dtype = numpy.dtype('>f4')
            complex_type = True
        elif pixel_type == 'RE16I_IM16I':
            dtype = numpy.dtype('>i2')
            complex_type = True
        elif pixel_type == 'AMP8I_PHS8I':
            dtype = numpy.dtype('>u1')
            complex_type = amp_phase_to_complex(meta.ImageData.AmpTable)
        else:
            raise ValueError('Pixel Type {} not recognized.'.format(pixel_type))

        rows_total = meta.ImageData.NumRows
        cols_total = meta.ImageData.NumCols
        bounds = numpy.zeros((self._nitf_details.img_segment_offsets.size, 4), dtype=numpy.uint64)
        p_row_start, p_row_end, p_col_start, p_col_end = None, None, None, None
        for i, (rows, cols) in enumerate(zip(self._nitf_details.img_segment_rows,
                                             self._nitf_details.img_segment_columns)):
            if i == 0:
                cur_row_start, cur_row_end = 0, rows
                cur_col_start, cur_col_end = 0, cols
            elif p_row_end == rows_total:
                cur_row_start, cur_row_end = 0, rows
                cur_col_start, cur_col_end = p_col_end, p_col_end + rows
            else:
                cur_row_start, cur_row_end = p_row_end, p_row_end + cols
                cur_col_start, cur_col_end = p_col_start, p_col_end

            if not (rows == cur_row_end - cur_row_start and cols == cur_col_end - cur_col_start):
                raise ValueError('Failed at calculating bounds entry {}.'.format(i))
            bounds[i] = (cur_row_start, cur_row_end, cur_col_start, cur_col_end)
            p_row_start, p_row_end, p_col_start, p_col_end = cur_row_start, cur_row_end, cur_col_start, cur_col_end

        if not (bounds[-1, 1] == rows_total and bounds[-1, 3] == cols_total):
            raise ValueError('Bounds final entry {} does not match sicd size '
                             '({}, {})'.format(bounds[-1], rows_total, cols_total))

        offsets = self._nitf_details.img_segment_offsets.copy()
        return MultiSegmentChipper(
            self._nitf_details.file_name, bounds, offsets, dtype,
            symmetry=(False, False, False), complex_type=complex_type,
            bands_ip=1)


#######
#  The actual writing implementation

def _validate_input(data):
    # type: (numpy.ndarray) -> tuple
    if not isinstance(data, numpy.ndarray):
        raise ValueError('Requires a numpy.ndarray, got {}'.format(type(data)))
    if data.dtype.name not in ('complex64', 'complex128'):
        raise ValueError('Requires a numpy.ndarray of complex dtype, got {}'.format(data.dtype.name))
    if len(data.shape) != 2:
        raise ValueError('Requires a two-dimensional numpy.ndarray, got {}'.format(data.shape))

    new_shape = (data.shape[0], data.shape[1], 2)
    return new_shape


def complex_to_amp_phase(lookup_table):
    """
    This constructs the function to convert from complex64 or 128 to AMP8I_PHS8I format data.

    Parameters
    ----------
    lookup_table : numpy.ndarray

    Returns
    -------
    callable
    """

    _validate_lookup(lookup_table)

    def converter(data):
        new_shape = _validate_input(data)
        out = numpy.zeros(new_shape, dtype=numpy.uint8)
        # NB: for numpy before 1.10, digitize requires 1-d
        out[:, :, 0] = numpy.digitize(numpy.abs(data).ravel(), lookup_table, right=False).reshape(data.shape)
        out[:, :, 1] = numpy.arctan2(data.real, data.imag)*(256/(2*numpy.pi))
        # truncation takes care of properly rolling negative to positive
        return out

    return converter


def complex_to_int(data):
    """
    This converts from complex64 or 128 data to int16 data.

    Parameters
    ----------
    data : numpy.ndarray

    Returns
    -------
    numpy.ndarray
    """

    # TODO: this is naive. Scaling down to 16-bit limits requires thought.
    new_shape = _validate_input(data)

    if data.dtype.name == 'complex128':
        view_dtype = numpy.float64
    else:
        view_dtype = numpy.float32

    i16_info = numpy.iinfo(numpy.int16)  # for getting max/min type values
    data_view = data.view(dtype=view_dtype).reshape(new_shape)
    out = numpy.zeros(new_shape, dtype=numpy.int16)
    out[:] = numpy.round(numpy.clip(data_view, i16_info.min, i16_info.max))
    # this is nonsense without the clip - gets cast to int64 and then truncated.
    # should we round? Without it, it will be the floor, I believe.
    return out


def extract_clas(sicd):
    """
    Extract the classification string from a SICD as appropriate for NITF Security
    tags CLAS attribute.

    Parameters
    ----------
    sicd : SICDType

    Returns
    -------
    str
    """
    if sicd.CollectionInfo is None or sicd.CollectionInfo.Classification is None:
        return 'U'

    c_str = sicd.CollectionInfo.Classification

    if 'UNCLASS' in c_str.upper():
        return 'U'
    elif 'CONFIDENTIAL' in c_str.upper():
        return 'C'
    elif 'TOP SECRET' in c_str.upper():
        return 'T'
    elif 'SECRET' in c_str.upper():
        return 'S'
    elif 'FOUO' in c_str.upper() or 'RESTRICTED' in c_str.upper():
        return 'R'
    else:
        logging.critical('Unclear how to extract CLAS for classification string {}. '
                         'Should be set appropriately.'.format(c_str))
        return 'U'


class SICDWriter(NITFWriter):
    """
    Writer class for a SICD file - a NITF file containing complex radar data and 
    SICD data extension. 
    """

    __slots__ = ('_sicd_meta', )

    def __init__(self, file_name, sicd_meta):
        """

        Parameters
        ----------
        file_name : str
        sicd_meta : sarpy.io.complex.sicd_elements.SICD.SICDType
        """
        self._sicd_meta = validate_sicd_for_writing(sicd_meta)
        self._security_tags = None
        self._nitf_header = None
        self._img_groups = None
        self._img_details = None
        self._des_details = None
        self._shapes = ((self.sicd_meta.ImageData.NumRows, self.sicd_meta.ImageData.NumCols), )
        super(SICDWriter, self).__init__(file_name)

    @property
    def sicd_meta(self):
        """
        sarpy.io.complex.sicd_elements.SICD.SICDType: The sicd metadata
        """

        return self._sicd_meta

    def default_security_tags(self):
        """
        Returns a NITF security tags object with `CLAS` and `CODE`
        attributes set from the SICD.CollectionInfo.Classification value.

        It is expected that output from this will be modified as appropriate
        and used to set ONLY specific security tags in `data_extension_header` or
        elements of `image_segment_headers`.

        If simultaneous modification of all security tags attributes for the entire
        SICD is the goal, then directly modify the value(s) using `security_tags`.

        Returns
        -------
        sarpy.io.nitf.security.NITFSecurityTags
        """

        def get_basic_args():
            out = {}
            if hasattr(self._sicd_meta, '_NITF') and isinstance(self._sicd_meta._NITF, dict):
                sec_tags = self._sicd_meta._NITF.get('Security', {})
                # noinspection PyProtectedMember
                for fld in NITFSecurityTags._ordering:
                    if fld in sec_tags:
                        out[fld] = sec_tags[fld]
            return out

        def get_clas():
            if 'CLAS' in args:
                return
            args['CLAS'] = extract_clas(self.sicd_meta)

        def get_code(in_str):
            if 'CODE' in args:
                return

            code = re.search('(?<=/)[^/].*', in_str)
            if code is not None:
                args['CODE'] = code.group()

        args = get_basic_args()
        if self._sicd_meta.CollectionInfo is not None:
            get_clas()
            get_code(self._sicd_meta.CollectionInfo.Classification)
        return NITFSecurityTags(**args)

    def _create_security_tags(self):
        self._security_tags = self.default_security_tags()

    def _get_ftitle(self):  # type: () -> str
        ftitle = None
        if hasattr(self._sicd_meta, '_NITF') and isinstance(self._sicd_meta._NITF, dict):
            ftitle = self._sicd_meta._NITF.get('SUGGESTED_NAME', None)
        if ftitle is None:
            ftitle = self._sicd_meta.get_suggested_name(1)
        if ftitle is None and self._sicd_meta.CollectionInfo is not None and \
                self._sicd_meta.CollectionInfo.CoreName is not None:
            ftitle = 'SICD: {}'.format(self._sicd_meta.CollectionInfo.CoreName)
        if ftitle is None:
            ftitle = 'SICD: Unknown'
        return ftitle

    def _get_fdt(self):
        return re.sub(r'[^0-9]', '', str(self.sicd_meta.ImageCreation.DateTime.astype('datetime64[s]')))

    def _get_ostaid(self):
        ostaid = 'Unknown'
        if hasattr(self._sicd_meta, '_NITF') and isinstance(self._sicd_meta._NITF, dict):
            ostaid = self._sicd_meta._NITF.get('OSTAID', 'Unknown')
        return ostaid

    def _image_parameters(self):
        """
        Get the image parameters.

        Returns
        -------
        (int, numpy.dtype, Union[bool, callable], str, tuple, tuple)
            pixel_size - the size of each pixel in bytes.
            dtype - the data type.
            complex_type -
            pv_type - the pixel type string.
            isubcat - the image subcategory.
            im_segments - Segmentation of the form `((row start, row end, column start, column end))`
        """

        pixel_type = self.sicd_meta.ImageData.PixelType  # required to be defined
        # NB: SICDs are required to be stored as big-endian, so the endian-ness
        #   of the memmap must be explicit
        if pixel_type == 'RE32F_IM32F':
            pv_type, isubcat = 'R', ('I', 'Q')
            pixel_size = 8
            dtype = numpy.dtype('>f4')
            complex_type = True
        elif pixel_type == 'RE16I_IM16I':
            pv_type, isubcat = 'SI', ('I', 'Q')
            pixel_size = 4
            dtype = numpy.dtype('>i2')
            complex_type = complex_to_int
        else:  # pixel_type == 'AMP8I_PHS8I':
            pv_type, isubcat = 'INT', ('M', 'P')
            pixel_size = 2
            dtype = numpy.dtype('>u1')
            complex_type = complex_to_amp_phase(self.sicd_meta.ImageData.AmpTable)
        image_segment_limits = image_segmentation(
            self.sicd_meta.ImageData.NumRows, self.sicd_meta.ImageData.NumCols, pixel_size)
        return pixel_size, dtype, complex_type, pv_type, isubcat, image_segment_limits

    def _create_image_segment_details(self):
        super(SICDWriter, self)._create_image_segment_details()

        pixel_size, dtype, complex_type, pv_type, isubcat, image_segment_limits = self._image_parameters()
        img_groups = tuple(range(len(image_segment_limits)))
        self._img_groups = (img_groups, )

        ftitle = self._get_ftitle()
        idatim = ' '
        if self.sicd_meta.Timeline is not None and self.sicd_meta.Timeline.CollectStart is not None:
            idatim = re.sub(r'[^0-9]', '', str(self.sicd_meta.Timeline.CollectStart.astype('datetime64[s]')))

        isource = 'SICD: Unknown Collector'
        if self.sicd_meta.CollectionInfo is not None and self.sicd_meta.CollectionInfo.CollectorName is not None:
            isource = 'SICD: {}'.format(self.sicd_meta.CollectionInfo.CollectorName)

        icp, rows, cols = None, None, None
        if self.sicd_meta.GeoData is not None and self.sicd_meta.GeoData.ImageCorners is not None:
            # noinspection PyTypeChecker
            icp = self.sicd_meta.GeoData.ImageCorners.get_array(dtype=numpy.float64)
            rows = self.sicd_meta.ImageData.NumRows
            cols = self.sicd_meta.ImageData.NumCols
        abpp = 4*pixel_size
        bands = [ImageBand(ISUBCAT=entry) for entry in isubcat]

        img_details = []

        for i, entry in enumerate(image_segment_limits):
            this_rows = entry[1]-entry[0]
            this_cols = entry[3]-entry[2]
            subhead = ImageSegmentHeader(
                IID1='SICD{0:03d}'.format(0 if len(image_segment_limits) == 1 else i+1),
                IDATIM=idatim,
                IID2=ftitle,
                ISORCE=isource,
                IREP='NODISPLY',
                ICAT='SAR',
                NROWS=this_rows,
                NCOLS=this_cols,
                PVTYPE=pv_type,
                ABPP=abpp,
                IGEOLO=interpolate_corner_points_string(numpy.array(entry, dtype=numpy.int64), rows, cols, icp),
                NPPBH=get_npp_block(this_cols),
                NPPBV=get_npp_block(this_rows),
                NBPP=abpp,
                IDLVL=i+1,
                IALVL=i,
                ILOC='{0:05d}{1:05d}'.format(entry[0], entry[2]),
                Bands=ImageBands(values=bands),
                Security=self._security_tags)
            img_details.append(ImageDetails(1, dtype, complex_type, entry, subhead))

        self._img_details = tuple(img_details)

    def _create_data_extension_details(self):
        super(SICDWriter, self)._create_data_extension_details()
        uh_args = self.sicd_meta.get_des_details()

        desshdt = str(self.sicd_meta.ImageCreation.DateTime.astype('datetime64[s]'))
        if desshdt[-1] != 'Z':
            desshdt += 'Z'
        uh_args['DESSHDT'] = desshdt

        desshlpg = ''
        if self.sicd_meta.GeoData is not None and self.sicd_meta.GeoData.ImageCorners is not None:
            # noinspection PyTypeChecker
            icp = self.sicd_meta.GeoData.ImageCorners.get_array(dtype=numpy.float64)
            temp = []
            for entry in icp:
                temp.append('{0:0=+12.8f}{1:0=+13.8f}'.format(entry[0], entry[1]))
            temp.append(temp[0])
            desshlpg = ''.join(temp)
        uh_args['DESSHLPG'] = desshlpg

        subhead = DataExtensionHeader(
            Security=self._security_tags,
            UserHeader=SICDDESSubheader(**uh_args))

        self._des_details = (
            DESDetails(subhead, self.sicd_meta.to_xml_bytes(tag='SICD')), )
