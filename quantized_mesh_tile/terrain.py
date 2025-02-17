""" This module defines the :class:`quantized_mesh_tile.terrain.TerrainTile`.
More information about the format specification can be found here:
https://github.com/AnalyticalGraphicsInc/quantized-mesh

Reference
---------
"""

import gzip
import io
import os
from collections import OrderedDict

from . import horizon_occlusion_point as occ
from .bbsphere import BoundingSphere
from .topology import TerrainTopology
from .utils import (decodeIndices, encodeIndices, gzipFileObject, octDecode,
                    octEncode, packEntry, packIndices, ungzipFileObject,
                    unpackEntry, zigZagDecode, zigZagEncode)

# For a tile of 256px * 256px
TILEPXS = 65536


def lerp(p, q, time):
    return ((1.0 - time) * p) + (time * q)


class TerrainTile(object):
    """
    The main class to read and write a terrain tile.

    Constructor arguments:

    ``west``

        The longitude at the western edge of the tile. Default is `-1.0`.

    ``east``

        The longitude at the eastern edge of the tile. Default is `1.0`.

    ``south``

        The latitude at the southern edge of the tile. Default is `-1.0`.

    ``north``

        The latitude at the northern edge of the tile. Default is `1.0`.

    ``topology``

        The topology of the mesh which but be an instance of
        :class:`quantized_mesh_tile.topology.TerrainTopology`. Default is `None`.

    ``watermask``
        A water mask list (Optional). Adds rendering water effect.
        The water mask list is either one byte, `[0]` for land and `[255]` for
        water, either a list of 256*256 values ranging from 0 to 255.
        Values in the mask are defined from north-to-south and west-to-east.
        Per default no watermask is applied. Note that the water mask effect depends on
        the texture of the raster layer drapped over your terrain.
        Default is `[]`.

    Usage examples::

        from quantized_mesh_tile.terrain import TerrainTile
        from quantized_mesh_tile.topology import TerrainTopology
        from quantized_mesh_tile.global_geodetic import GlobalGeodetic

        # The tile coordinates
        x = 533
        y = 383
        z = 9
        geodetic = GlobalGeodetic(True)
        [west, south, east, north] = geodetic.TileBounds(x, y, z)

        # Read a terrain tile (unzipped)
        tile = TerrainTile(west=west, south=south, east=east, north=north)
        tile.fromFile('mytile.terrain')

        # Write a terrain tile locally from scratch (lon/lat/height)
        wkts = [
            'POLYGON Z ((7.3828125 44.6484375 303.3, ' +
                        '7.3828125 45.0 320.2, ' +
                        '7.5585937 44.82421875 310.2, ' +
                        '7.3828125 44.6484375 303.3))',
            'POLYGON Z ((7.3828125 44.6484375 303.3, ' +
                        '7.734375 44.6484375 350.3, ' +
                        '7.5585937 44.82421875 310.2, ' +
                        '7.3828125 44.6484375 303.3))',
            'POLYGON Z ((7.734375 44.6484375 350.3, ' +
                        '7.734375 45.0 330.3, ' +
                        '7.5585937 44.82421875 310.2, ' +
                        '7.734375 44.6484375 350.3))',
            'POLYGON Z ((7.734375 45.0 330.3, ' +
                        '7.5585937 44.82421875 310.2, ' +
                        '7.3828125 45.0 320.2, ' +
                        '7.734375 45.0 330.3))'
        ]
        topology = TerrainTopology(geometries=wkts)
        tile = TerrainTile(topology=topology)
        tile.toFile('mytile.terrain')

    """
    quantizedMeshHeader = OrderedDict([
        ['centerX', 'd'],  # 8bytes
        ['centerY', 'd'],
        ['centerZ', 'd'],
        ['minimumHeight', 'f'],  # 4bytes
        ['maximumHeight', 'f'],
        ['boundingSphereCenterX', 'd'],
        ['boundingSphereCenterY', 'd'],
        ['boundingSphereCenterZ', 'd'],
        ['boundingSphereRadius', 'd'],
        ['horizonOcclusionPointX', 'd'],
        ['horizonOcclusionPointY', 'd'],
        ['horizonOcclusionPointZ', 'd']
    ])

    vertexData = OrderedDict([
        # 4bytes -> determines the size of the 3 following arrays
        ['vertexCount', 'I'],
        ['uVertexCount', 'H'],  # 2bytes, unsigned short
        ['vVertexCount', 'H'],
        ['heightVertexCount', 'H']
    ])

    indexData16 = OrderedDict([
        ['triangleCount', 'I'],
        ['indices', 'H']
    ])
    indexData32 = OrderedDict([
        ['triangleCount', 'I'],
        ['indices', 'I']
    ])

    EdgeIndices16 = OrderedDict([
        ['westVertexCount', 'I'],
        ['westIndices', 'H'],
        ['southVertexCount', 'I'],
        ['southIndices', 'H'],
        ['eastVertexCount', 'I'],
        ['eastIndices', 'H'],
        ['northVertexCount', 'I'],
        ['northIndices', 'H']
    ])
    EdgeIndices32 = OrderedDict([
        ['westVertexCount', 'I'],
        ['westIndices', 'I'],
        ['southVertexCount', 'I'],
        ['southIndices', 'I'],
        ['eastVertexCount', 'I'],
        ['eastIndices', 'I'],
        ['northVertexCount', 'I'],
        ['northIndices', 'I']
    ])

    ExtensionHeader = OrderedDict([
        ['extensionId', 'B'],
        ['extensionLength', 'I']
    ])

    OctEncodedVertexNormals = OrderedDict([
        ['xy', 'B']
    ])

    WaterMask = OrderedDict([
        ['xy', 'B']
    ])

    BYTESPLIT = 65636

    # min and max quantized values for indices
    MIN = 0.0
    MAX = 32767.0

    # Coordinates are given in lon/lat WSG84
    def __init__(self, *args, **kwargs):
        self._west = kwargs.get('west', -1.0)
        self._east = kwargs.get('east', 1.0)
        self._south = kwargs.get('south', -1.0)
        self._north = kwargs.get('north', 1.0)
        self._longs = []
        self._lats = []
        self._heights = []
        self._triangles = []
        self._workingUnitLongitude = None
        self._workingUnitLatitude = None
        self._deltaHeight = None
        self.EPSG = 4326

        # Extensions
        self.vLight = []
        self.watermask = kwargs.get('watermask', [])
        self.hasWatermask = kwargs.get('hasWatermask', bool(self.watermask))

        self.header = OrderedDict()
        for k in TerrainTile.quantizedMeshHeader.keys():
            self.header[k] = 0.0
        self.u = []
        self.v = []
        self.h = []
        self.indices = []
        self.westI = []
        self.southI = []
        self.eastI = []
        self.northI = []

        topology = kwargs.get('topology')
        if topology is not None:
            self.fromTerrainTopology(topology)

    def __repr__(self):
        msg = 'Header: %s\n' % self.header
        # Output intermediate structure
        msg += '\nVertexCount: %s' % len(self.u)
        msg += '\nuVertex: %s' % self.u
        msg += '\nvVertex: %s' % self.v
        msg += '\nhVertex: %s' % self.h
        msg += '\nindexDataCount: %s' % len(self.indices)
        msg += '\nindexData: %s' % self.indices
        msg += '\nwestIndicesCount: %s' % len(self.westI)
        msg += '\nwestIndices: %s' % self.westI
        msg += '\nsouthIndicesCount: %s' % len(self.southI)
        msg += '\nsouthIndices: %s' % self.southI
        msg += '\neastIndicesCount: %s' % len(self.eastI)
        msg += '\neastIndices: %s' % self.eastI
        msg += '\nnorthIndicesCount: %s' % len(self.northI)
        msg += '\nnorthIndices: %s\n' % self.northI
        # Output coordinates
        msg += '\nNumber of triangles: %s' % (len(self.indices) // 3)
        msg += '\nTriangles coordinates in EPSG %s' % self.EPSG
        msg += '\n%s' % self.getTrianglesCoordinates()

        return msg

    @property
    def bounds(self):
        return [self._west, self._south, self._east, self._north]

    def getContentType(self):
        """
        A method to determine the content type of a tile.
        """
        baseContent = 'application/vnd.quantized-mesh'
        if self.hasLighting and self.hasWatermask:
            return baseContent + ';extensions=octvertexnormals-watermask'
        elif self.hasLighting:
            return baseContent + ';extensions=octvertexnormals'
        elif self.hasWatermask:
            return baseContent + ';extensions=watermask'
        else:
            return baseContent

    def getVerticesCoordinates(self):
        """
        A method to retrieve the coordinates of the vertices in lon,lat,height.
        """
        self._computeVerticesCoordinates()
        coordinates = []
        for i, lon in enumerate(self._longs):
            coordinates.append((lon, self._lats[i], self._heights[i]))
        return coordinates

    def getTrianglesCoordinates(self):
        """
        A method to retrieve triplet of coordinates representing the triangles
        in lon,lat,height.
        """
        self._computeVerticesCoordinates()
        triangles = []
        nbTriangles = len(self.indices)
        if nbTriangles % 3 != 0:
            raise Exception('Corrupted tile')
        for i in range(0, nbTriangles - 1, 3):
            vi1 = self.indices[i]
            vi2 = self.indices[i + 1]
            vi3 = self.indices[i + 2]
            triangle = (
                (self._longs[vi1],
                 self._lats[vi1],
                 self._heights[vi1]),
                (self._longs[vi2],
                 self._lats[vi2],
                 self._heights[vi2]),
                (self._longs[vi3],
                 self._lats[vi3],
                 self._heights[vi3])
            )
            triangles.append(triangle)
        return triangles

    def _computeVerticesCoordinates(self):
        """
        A private method to compute the vertices coordinates.
        """
        if not self._longs:
            for u in self.u:
                self._longs.append(
                    lerp(self._west, self._east, u / self.MAX))
            for v in self.v:
                self._lats.append(
                    lerp(self._south, self._north, v / self.MAX))
            for h in self.h:
                self._heights.append(
                    lerp(
                        self.header['minimumHeight'],
                        self.header['maximumHeight'],
                        h / self.MAX
                    )
                )

    def fromBytesIO(self, f, hasLighting=False, hasWatermask=False):
        """
        A method to read a terrain tile content.

        Arguments:

        ``f``

            An instance of io.BytesIO containing the terrain data. (Required)

        ``hasLighting``

            Indicate if the tile contains lighting information. Default is ``False``.

        ``hasWatermask``

            Indicate if the tile contains watermask information. Default is ``False``.
        """
        self.hasLighting = hasLighting
        self.hasWatermask = hasWatermask
        # Header
        for k, v in TerrainTile.quantizedMeshHeader.items():
            self.header[k] = unpackEntry(f, v)

        # Vertices
        vertexCount = unpackEntry(f, TerrainTile.vertexData['vertexCount'])
        for ud in self._iterUnpackAndDecodeVertices(
                f, vertexCount, TerrainTile.vertexData['uVertexCount']):
            self.u.append(ud)
        for vd in self._iterUnpackAndDecodeVertices(
                f, vertexCount, TerrainTile.vertexData['vVertexCount']):
            self.v.append(vd)
        for hd in self._iterUnpackAndDecodeVertices(
                f, vertexCount, TerrainTile.vertexData['heightVertexCount']):
            self.h.append(hd)

        # Indices
        meta = TerrainTile.indexData16
        if vertexCount > TerrainTile.BYTESPLIT:
            meta = TerrainTile.indexData32
        triangleCount = unpackEntry(f, meta['triangleCount'])
        ind = [
            index for index
            in self._iterUnpackIndices(f, triangleCount * 3, meta['indices'])]
        self.indices = decodeIndices(ind)

        meta = TerrainTile.EdgeIndices16
        if vertexCount > TerrainTile.BYTESPLIT:
            meta = TerrainTile.indexData32
        # Edges (vertices on the edge of the tile)
        westIndicesCount = unpackEntry(f, meta['westVertexCount'])
        for wi in self._iterUnpackIndices(f, westIndicesCount, meta['westIndices']):
            self.westI.append(wi)

        southIndicesCount = unpackEntry(f, meta['southVertexCount'])
        for si in self._iterUnpackIndices(f, southIndicesCount, meta['southIndices']):
            self.southI.append(si)

        eastIndicesCount = unpackEntry(f, meta['eastVertexCount'])
        for ei in self._iterUnpackIndices(f, eastIndicesCount, meta['eastIndices']):
            self.eastI.append(ei)

        northIndicesCount = unpackEntry(f, meta['northVertexCount'])
        for ni in self._iterUnpackIndices(f, northIndicesCount, meta['northIndices']):
            self.northI.append(ni)

        if self.hasLighting:
            # One byte of padding
            # Light extension header
            meta = TerrainTile.ExtensionHeader
            extensionId = unpackEntry(f, meta['extensionId'])
            if extensionId == 1:
                extensionLength = unpackEntry(f, meta['extensionLength'])

                for xy in self._iterUnpackAndDecodeLight(
                        f, extensionLength, TerrainTile.OctEncodedVertexNormals['xy']):
                    self.vLight.append(xy)

        if self.hasWatermask:
            meta = TerrainTile.ExtensionHeader
            extensionId = unpackEntry(f, meta['extensionId'])
            if extensionId == 2:
                extensionLength = unpackEntry(f, meta['extensionLength'])
                for row in self._iterUnpackWatermaskRow(
                        f, extensionLength, TerrainTile.WaterMask['xy']):
                    self.watermask.append(row)

        # data = f.read(1)
        # if data:
        #     raise Exception('Should have reached end of file, but didn\'t')

    @staticmethod
    def _iterUnpackAndDecodeVertices(f, vertexCount, structType):
        """
        A private method to itertatively unpack and decode indices.
        """
        i = 0
        # Delta decoding
        delta = 0
        while i != vertexCount:
            delta += zigZagDecode(unpackEntry(f, structType))
            yield delta
            i += 1

    @staticmethod
    def _iterUnpackIndices(f, indicesCount, structType):
        """
        A private method to iteratively unpack indices
        """
        i = 0
        while i != indicesCount:
            yield unpackEntry(f, structType)
            i += 1

    @staticmethod
    def _iterUnpackAndDecodeLight(f, extensionLength, structType):
        """
        A private method to iteratively unpack light vector.
        """
        i = 0
        xyCount = extensionLength / 2
        while i != xyCount:
            yield octDecode(
                unpackEntry(
                    f, structType),
                unpackEntry(
                    f, structType)
            )
            i += 1

    @staticmethod
    def _iterUnpackWatermaskRow(f, extensionLength, structType):
        """
        A private method to iteratively unpack watermask rows
        """
        i = 0
        xyCount = 0
        row = []
        while xyCount != extensionLength:
            row.append(unpackEntry(f, structType))
            if i == 255:
                yield row
                i = 0
                row = []
            else:
                i += 1
            xyCount += 1
        if row:
            yield row

    def fromFile(self, filePath, hasLighting=False, hasWatermask=False, gzipped=False):
        """
        A method to read a terrain tile file. It is assumed that the tile unzipped.

        Arguments:

        ``filePath``

            An absolute or relative path to a quantized-mesh terrain tile. (Required)

        ``hasLighting``

            Indicate if the tile contains lighting information. Default is ``False``.

        ``hasWatermask``

            Indicate if the tile contains watermask information. Default is ``False``.

        ``gzipped``

            Indicate if the tile content is gzipped. Default is ``False``.
        """
        with open(filePath, 'rb') as f:
            if gzipped:
                f = ungzipFileObject(f)
            self.fromBytesIO(f, hasLighting=hasLighting,
                             hasWatermask=hasWatermask)

    def toBytesIO(self, gzipped=False):
        """
        A method to write the terrain tile data to a file-like object (a string buffer).

        Arguments:

        ``gzipped``

            Indicate if the content should be gzipped. Default is ``False``.
        """
        f = io.BytesIO()
        self._writeTo(f)
        if gzipped:
            f = gzipFileObject(f)
        return f

    def toFile(self, filePath, gzipped=False):
        """
        A method to write the terrain tile data to a physical file.

        Argument:

        ``filePath``

            An absolute or relative path to write the terrain tile. (Required)

        ``gzipped``

            Indicate if the content should be gzipped. Default is ``False``.
        """
        if os.path.isfile(filePath):
            raise IOError('File %s already exists' % filePath)

        if not gzipped:
            with open(filePath, 'wb') as f:
                self._writeTo(f)
        else:
            with gzip.open(filePath, 'wb') as f:
                self._writeTo(f)
        

    def toOurFile(self, filePath, gzipped=False):
        """
        
        A method to write the terrain tile data to a physical file.
        Modified to overwrite the file if it exists.

        Argument:

        ``filePath``

            An absolute or relative path to write the terrain tile. (Required)

        ``gzipped``

            Indicate if the content should be gzipped. Default is ``False``.
        """
        # if os.path.isfile(filePath):
        #     with open(filePath, 'wb') as f:
        #         self._writeTo(f)

        if not gzipped:
            with open(filePath, 'wb') as f:
                self._writeTo(f)
        else:
            with gzip.open(filePath, 'wb') as f:
                self._writeTo(f)

    def _getWorkingUnitLatitude(self):
        if not self._workingUnitLatitude:
            self._workingUnitLatitude = self.MAX / (self._north - self._south)
        return self._workingUnitLatitude

    def _getWorkingUnitLongitude(self):
        if not self._workingUnitLongitude:
            self._workingUnitLongitude = self.MAX / (self._east - self._west)
        return self._workingUnitLongitude

    def _getDeltaHeight(self):
        if not self._deltaHeight:
            maxHeight = self.header['maximumHeight']
            minHeight = self.header['minimumHeight']
            self._deltaHeight = maxHeight - minHeight
        return self._deltaHeight

    def _quantizeLatitude(self, latitude):
        return int(round((latitude - self._south) *
                         self._getWorkingUnitLatitude()))

    def _quantizeLongitude(self, longitude):
        return int(round((longitude - self._west) *
                         self._getWorkingUnitLongitude()))

    def _quantizeHeight(self, height):
        deniv = self._getDeltaHeight()
        # In case a tile is completely flat
        if deniv == 0:
            h = 0
        else:
            workingUnitHeight = self.MAX / deniv
            h = int(round((height - self.header['minimumHeight']) * workingUnitHeight))
        return h

    def _dequantizeHeight(self, h):
        """
        Private helper method to convert quantized tile (h) values to real world height
        values
        :param h: the quantized height value
        :return: the height in ground units (meter)
        """
        return lerp(self.header['minimumHeight'],
                    self.header['maximumHeight'],
                    h / self.MAX)

    def _writeTo(self, f):
        """
        A private method to write the terrain tile to a file or file-like object.
        """
        # Header
        for k, v in TerrainTile.quantizedMeshHeader.items():
            f.write(packEntry(v, self.header[k]))

        # Delta decoding
        vertexCount = len(self.u)
        # Vertices
        f.write(packEntry(TerrainTile.vertexData['vertexCount'], vertexCount))
        # Move the initial value
        f.write(
            packEntry(
                TerrainTile.vertexData['uVertexCount'], zigZagEncode(self.u[0]))
        )
        for i in range(0, vertexCount - 1):
            ud = self.u[i + 1] - self.u[i]
            f.write(
                packEntry(TerrainTile.vertexData['uVertexCount'], zigZagEncode(ud)))
        f.write(
            packEntry(
                TerrainTile.vertexData['uVertexCount'], zigZagEncode(self.v[0]))
        )
        for i in range(0, vertexCount - 1):
            vd = self.v[i + 1] - self.v[i]
            f.write(
                packEntry(TerrainTile.vertexData['vVertexCount'], zigZagEncode(vd)))
        f.write(
            packEntry(
                TerrainTile.vertexData['uVertexCount'], zigZagEncode(self.h[0]))
        )
        for i in range(0, vertexCount - 1):
            hd = self.h[i + 1] - self.h[i]
            f.write(
                packEntry(
                    TerrainTile.vertexData['heightVertexCount'], zigZagEncode(hd))
            )

        # Indices
        meta = TerrainTile.indexData16
        if vertexCount > TerrainTile.BYTESPLIT:
            meta = TerrainTile.indexData32

        f.write(packEntry(meta['triangleCount'], len(self.indices) // 3))
        ind = encodeIndices(self.indices)
        packIndices(f, meta['indices'], ind)

        meta = TerrainTile.EdgeIndices16
        if vertexCount > TerrainTile.BYTESPLIT:
            meta = TerrainTile.EdgeIndices32

        f.write(packEntry(meta['westVertexCount'], len(self.westI)))
        for wi in self.westI:
            f.write(packEntry(meta['westIndices'], wi))

        f.write(packEntry(meta['southVertexCount'], len(self.southI)))
        for si in self.southI:
            f.write(packEntry(meta['southIndices'], si))

        f.write(packEntry(meta['eastVertexCount'], len(self.eastI)))
        for ei in self.eastI:
            f.write(packEntry(meta['eastIndices'], ei))

        f.write(packEntry(meta['northVertexCount'], len(self.northI)))
        for ni in self.northI:
            f.write(packEntry(meta['northIndices'], ni))

        # Extension header for light
        if len(self.vLight) > 0:
            self.hasLighting = True
            meta = TerrainTile.ExtensionHeader
            # Extension header ID is 1 for lightening
            f.write(packEntry(meta['extensionId'], 1))
            # Unsigned char size len is 1
            f.write(packEntry(meta['extensionLength'], 2 * vertexCount))

            metaV = TerrainTile.OctEncodedVertexNormals
            for i in range(0, vertexCount):
                x, y = octEncode(self.vLight[i])
                f.write(packEntry(metaV['xy'], x))
                f.write(packEntry(metaV['xy'], y))

        if self.watermask:
            self.hasWatermask = True
            # Extension header ID is 2 for watermark
            meta = TerrainTile.ExtensionHeader
            f.write(packEntry(meta['extensionId'], 2))
            # Extension header meta
            nbRows = len(self.watermask)
            if nbRows > 1:
                # Unsigned char size len is 1
                f.write(packEntry(meta['extensionLength'], TILEPXS))
                if nbRows != 256:
                    raise Exception(
                        'Unexpected number of rows for the watermask: %s' % nbRows
                    )
                # From North to South
                for i in range(0, nbRows):
                    x = self.watermask[i]
                    if len(x) != 256:
                        raise Exception(
                            'Unexpected number of columns for the watermask: %s' % len(
                                x)
                        )
                    # From West to East
                    for y in x:
                        f.write(packEntry(TerrainTile.WaterMask['xy'], int(y)))
            else:
                f.write(packEntry(meta['extensionLength'], 1))
                if self.watermask[0][0] is None:
                    self.watermask[0][0] = 0
                f.write(
                    packEntry(TerrainTile.WaterMask['xy'], int(self.watermask[0][0])))

    def fromTerrainTopology(self, topology, bounds=None):
        """
        A method to prepare a terrain tile data structure.

        Arguments:

        ``topology``

            The topology of the mesh which must be an instance of
            :class:`quantized_mesh_tile.topology.TerrainTopology`. (Required)

        ``bounds``

            The bounds of a the terrain tile. (west, south, east, north)
            If not defined, the bounds defined during initialization will be used.
            If no bounds are provided, then the bounds
            are extracted from the topology object.

        """
        if not isinstance(topology, TerrainTopology):
            raise Exception(
                'topology object must be an instance of TerrainTopology')

        # If the bounds are not provided use
        # topology extent instead
        if bounds is not None:
            self._west = bounds[0]
            self._east = bounds[2]
            self._south = bounds[1]
            self._north = bounds[3]
        elif set([self._west, self._south, self._east, self._north]).difference(
                set([-1.0, -1.0, 1.0, 1.0])):
            # Bounds already defined earlier
            pass
        else:
            # Set tile bounds
            self._west = topology.minLon
            self._east = topology.maxLon
            self._south = topology.minLat
            self._north = topology.maxLat

        bSphere = BoundingSphere()
        bSphere.fromPoints(topology.cartesianVertices)

        ecefMinX = topology.ecefMinX
        ecefMinY = topology.ecefMinY
        ecefMinZ = topology.ecefMinZ
        ecefMaxX = topology.ecefMaxX
        ecefMaxY = topology.ecefMaxY
        ecefMaxZ = topology.ecefMaxZ

        # Center of the bounding box 3d
        centerCoords = [
            ecefMinX + (ecefMaxX - ecefMinX) * 0.5,
            ecefMinY + (ecefMaxY - ecefMinY) * 0.5,
            ecefMinZ + (ecefMaxZ - ecefMinZ) * 0.5
        ]

        occlusionPCoords = occ.fromPoints(topology.cartesianVertices, bSphere)

        for k in TerrainTile.quantizedMeshHeader.keys():
            if k == 'centerX':
                self.header[k] = centerCoords[0]
            elif k == 'centerY':
                self.header[k] = centerCoords[1]
            elif k == 'centerZ':
                self.header[k] = centerCoords[2]
            elif k == 'minimumHeight':
                self.header[k] = topology.minHeight
            elif k == 'maximumHeight':
                self.header[k] = topology.maxHeight
            elif k == 'boundingSphereCenterX':
                self.header[k] = bSphere.center[0]
            elif k == 'boundingSphereCenterY':
                self.header[k] = bSphere.center[1]
            elif k == 'boundingSphereCenterZ':
                self.header[k] = bSphere.center[2]
            elif k == 'boundingSphereRadius':
                self.header[k] = bSphere.radius
            elif k == 'horizonOcclusionPointX':
                self.header[k] = occlusionPCoords[0]
            elif k == 'horizonOcclusionPointY':
                self.header[k] = occlusionPCoords[1]
            elif k == 'horizonOcclusionPointZ':
                self.header[k] = occlusionPCoords[2]

        # High watermark encoding performed during toFile
        self.u = [self._quantizeLongitude(longitude) for longitude in topology.uVertex]
        self.v = [self._quantizeLatitude(latitude) for latitude in topology.vVertex]
        self.h = [self._quantizeHeight(height) for height in topology.hVertex]
        self.indices = topology.indexData

        # List all the vertices on the edge of the tile
        # Use quantized values to determine if an indice belong to a tile edge
        for indice in self.indices:
            x = self.u[indice]
            y = self.v[indice]

            if x == self.MIN and indice not in self.westI:
                self.westI.append(indice)
            elif x == self.MAX and indice not in self.eastI:
                self.eastI.append(indice)

            if y == self.MIN and indice not in self.southI:
                self.southI.append(indice)
            elif y == self.MAX and indice not in self.northI:
                self.northI.append(indice)

        self.hasLighting = topology.hasLighting
        if self.hasLighting:
            self.vLight = topology.verticesUnitVectors
