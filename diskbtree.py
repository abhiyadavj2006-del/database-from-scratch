#!/usr/bin/env python3
"""
diskbtree.py — a B-tree whose nodes are pages in the pager file.

This is the mkdb storage idea in Python: one B-tree node per 4 KiB
slotted page, child pointers are page numbers, and the table itself
is a B-tree keyed by its primary key (index-organized storage, like
SQLite). Secondary indexes are B-trees keyed by (indexed value,
primary key), so an index lookup is two O(log n) searches: index
B-tree, then table B-tree.

Differences from mkdb, kept deliberately:
  * mkdb uses a B*-tree with balance-on-demand (it redistributes
    cells to siblings before splitting). This is a classic CLRS
    B-tree: a node that no longer fits splits at its median key,
    which moves up to the parent. Same O(log n), simpler to verify.
  * mkdb implements full B-tree delete with merging/borrowing.
    This implementation deletes by *tombstone*: the cell stays in
    place, flagged deleted, and is skipped by every search and scan.
    Re-inserting the same key reuses the cell. Space is only
    reclaimed when the whole tree is freed (DROP TABLE / DROP INDEX).
    That is a real technique (LSM engines delete exactly this way),
    and it keeps delete obviously correct.
  * Keys are Python values (int / float / str / bool / None / list),
    encoded as JSON; comparisons decode first, with an explicit type
    rank so values of different types never compare by accident.
    Sentinels _MINUS_INF / _PLUS_INF bound range scans.

Cell values are raw bytes. Values longer than a quarter of a page
are stored with overflow pages: the first chunk stays in the cell
and the cell's last 4 bytes are the first overflow page number.
"""

from __future__ import annotations

import json
import struct

from pager import (
    CELL_FLAG_OVERFLOW,
    CELL_FLAG_TOMBSTONE,
    CELL_HEADER,
    NODE_HEADER,
    NODE_HEADER_SIZE,
    OVERFLOW_HEADER,
    OVERFLOW_THRESHOLD,
    PAGE_SIZE,
    PAGE_TYPE_BTREE,
    PAGE_TYPE_OVERFLOW,
    SLOT,
    Pager,
)


class _Inf:
    """Range-scan bound that compares smaller / larger than everything."""

    def __init__(self, sign: int):
        self.sign = sign

    def __repr__(self):
        return "-inf" if self.sign < 0 else "+inf"


MINUS_INF = _Inf(-1)
PLUS_INF = _Inf(1)

# Type rank for cross-type key comparison (a column holds one type,
# but composite index keys mix the indexed value with the PK).
_TYPE_RANK = {type(None): 0, bool: 1, int: 2, float: 2, str: 3, list: 4}


def _cmp(a, b) -> int:
    """Total order over key values, with _Inf sentinels."""
    if isinstance(a, _Inf) or isinstance(b, _Inf):
        sa = a.sign if isinstance(a, _Inf) else 0
        sb = b.sign if isinstance(b, _Inf) else 0
        return (sa > sb) - (sa < sb)
    ra, rb = _TYPE_RANK.get(type(a), 5), _TYPE_RANK.get(type(b), 5)
    if ra != rb:
        return -1 if ra < rb else 1
    if isinstance(a, list) and isinstance(b, list):
        for x, y in zip(a, b):
            c = _cmp(x, y)
            if c:
                return c
        return (len(a) > len(b)) - (len(a) < len(b))
    if isinstance(a, bool) or isinstance(b, bool):
        return (a > b) - (a < b)
    return (a > b) - (a < b)


def _key_index(keys, key) -> int:
    """bisect position of key in the sorted decoded key list."""
    lo, hi = 0, len(keys)
    while lo < hi:
        mid = (lo + hi) // 2
        if _cmp(keys[mid], key) < 0:
            lo = mid + 1
        else:
            hi = mid
    return lo


def encode_key(value) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def decode_key(raw: bytes):
    return json.loads(raw.decode("utf-8"))


class Node:
    """An in-memory image of one B-tree node page."""

    __slots__ = ("leaf", "keys", "values", "flags", "children")

    def __init__(self, leaf: bool):
        self.leaf = leaf
        self.keys: list = []        # decoded Python values, sorted
        self.values: list[bytes] = []  # cell value bytes (raw)
        self.flags: list[int] = []  # per-cell flags
        self.children: list[int] = []  # page numbers, len(keys)+1


class DiskBTree:
    """B-tree persisted in the pager file. ``root`` is the page number
    of the root node; it changes when the root splits."""

    def __init__(self, pager: Pager, root: int):
        self.pager = pager
        self.root = root

    @classmethod
    def create(cls, pager: Pager) -> "DiskBTree":
        page = pager.alloc_page()
        tree = cls(pager, page)
        tree._write_node(page, Node(leaf=True))
        return tree

    # -- node (de)serialisation -------------------------------------------
    def _read_node(self, page: int) -> Node:
        raw = self.pager.read_page(page)
        ptype, leaf, n_cells, cell_start, right_child, _ = \
            NODE_HEADER.unpack(raw[:NODE_HEADER_SIZE])
        if ptype != PAGE_TYPE_BTREE:
            raise ValueError(f"page {page} is not a B-tree node")
        node = Node(leaf=bool(leaf))
        node.children = [0] * (n_cells + 1)
        node.children[-1] = right_child
        for i in range(n_cells):
            off, length = SLOT.unpack(
                raw[NODE_HEADER_SIZE + 4 * i: NODE_HEADER_SIZE + 4 * i + 4])
            klen, vlen, flags, child = CELL_HEADER.unpack(
                raw[off: off + CELL_HEADER.size])
            key = decode_key(raw[off + CELL_HEADER.size:
                                 off + CELL_HEADER.size + klen])
            value = raw[off + CELL_HEADER.size + klen:
                        off + CELL_HEADER.size + klen + vlen]
            node.keys.append(key)
            node.values.append(bytes(value))
            node.flags.append(flags)
            node.children[i] = child
        return node

    def _node_bytes(self, node: Node) -> bytes:
        cells = []
        for i, key in enumerate(node.keys):
            kb = encode_key(key)
            vb = node.values[i]
            child = 0 if node.leaf else node.children[i]
            cells.append(CELL_HEADER.pack(len(kb), len(vb), node.flags[i],
                                          child) + kb + vb)
        total = NODE_HEADER_SIZE + SLOT.size * len(cells) + sum(
            len(c) for c in cells)
        if total > PAGE_SIZE:
            raise ValueError("node does not fit in a page")
        raw = bytearray(PAGE_SIZE)
        NODE_HEADER.pack_into(
            raw, 0, PAGE_TYPE_BTREE, 1 if node.leaf else 0, len(cells),
            PAGE_SIZE - sum(len(c) for c in cells),
            0 if node.leaf else node.children[-1], b"\x00" * 6)
        # Cells grow left from the end of the page, in key order.
        pos = PAGE_SIZE
        for i, cell in enumerate(cells):
            pos -= len(cell)
            raw[pos: pos + len(cell)] = cell
            SLOT.pack_into(raw, NODE_HEADER_SIZE + 4 * i, pos, len(cell))
        return bytes(raw)

    def _write_node(self, page: int, node: Node) -> None:
        self.pager.write_page(page, self._node_bytes(node))

    def _fits(self, node: Node) -> bool:
        try:
            self._node_bytes(node)
            return True
        except ValueError:
            return False

    # -- overflow payloads ---------------------------------------------------
    def _store_value(self, raw: bytes):
        """Split a value across overflow pages if it is too big.
        Returns (cell_bytes, flags)."""
        if len(raw) <= OVERFLOW_THRESHOLD:
            return raw, 0
        first = raw[:OVERFLOW_THRESHOLD]
        rest = raw[OVERFLOW_THRESHOLD:]
        chunks = []
        while rest:
            chunks.append(rest[: PAGE_SIZE - OVERFLOW_HEADER.size])
            rest = rest[PAGE_SIZE - OVERFLOW_HEADER.size:]
        pages = [self.pager.alloc_page() for _ in chunks]
        for j, (page, chunk) in enumerate(zip(pages, chunks)):
            nxt = pages[j + 1] if j + 1 < len(pages) else 0
            buf = bytearray(PAGE_SIZE)
            OVERFLOW_HEADER.pack_into(buf, 0, PAGE_TYPE_OVERFLOW, 0,
                                      len(chunk), nxt)
            buf[OVERFLOW_HEADER.size:
                OVERFLOW_HEADER.size + len(chunk)] = chunk
            self.pager.write_page(page, bytes(buf))
        return first + struct.pack(">I", pages[0]), CELL_FLAG_OVERFLOW

    def _load_value(self, cell: bytes, flags: int) -> bytes:
        if not (flags & CELL_FLAG_OVERFLOW):
            return cell
        out = bytearray(cell[:-4])
        (page,) = struct.unpack(">I", cell[-4:])
        while page != 0:
            raw = self.pager.read_page(page)
            _t, _p, dlen, nxt = OVERFLOW_HEADER.unpack(
                raw[: OVERFLOW_HEADER.size])
            out.extend(raw[OVERFLOW_HEADER.size:
                           OVERFLOW_HEADER.size + dlen])
            page = nxt
        return bytes(out)

    def _free_value(self, cell: bytes, flags: int) -> None:
        if not (flags & CELL_FLAG_OVERFLOW):
            return
        (page,) = struct.unpack(">I", cell[-4:])
        while page != 0:
            raw = self.pager.read_page(page)
            _t, _p, _dlen, nxt = OVERFLOW_HEADER.unpack(
                raw[: OVERFLOW_HEADER.size])
            self.pager.free_page(page)
            page = nxt

    # -- search ----------------------------------------------------------------
    def search(self, key):
        found = self._find(self.root, key)
        if found is None:
            return None
        _, node, i = found
        if node.flags[i] & CELL_FLAG_TOMBSTONE:
            return None
        return self._load_value(node.values[i], node.flags[i])

    def _find(self, page: int, key):
        """Descend to the node holding key. Returns
        (page, node, index) or None when the key is absent."""
        node = self._read_node(page)
        i = _key_index(node.keys, key)
        if i < len(node.keys) and _cmp(node.keys[i], key) == 0:
            return page, node, i
        if node.leaf:
            return None
        return self._find(node.children[i], key)

    # -- insert ------------------------------------------------------------------
    def insert(self, key, value: bytes) -> None:
        cell, flags = self._store_value(value)
        promoted = self._insert(self.root, key, cell, flags)
        if promoted is not None:
            mkey, mval, mflags, right = promoted
            new_root = self.pager.alloc_page()
            node = Node(leaf=False)
            node.keys = [mkey]
            node.values = [mval]
            node.flags = [mflags]
            node.children = [self.root, right]
            self._write_node(new_root, node)
            self.root = new_root

    def _insert(self, page: int, key, cell: bytes, flags: int):
        """Insert into the subtree rooted at page.
        Returns None, or (median_key, median_value, median_flags,
        right_page) when this node split and the median must move up.
        """
        node = self._read_node(page)
        i = _key_index(node.keys, key)
        if i < len(node.keys) and _cmp(node.keys[i], key) == 0:
            # Key already here (live or tombstoned): replace in place.
            self._free_value(node.values[i], node.flags[i])
            node.values[i] = cell
            node.flags[i] = flags
            self._write_node(page, node)
            return None
        if node.leaf:
            node.keys.insert(i, key)
            node.values.insert(i, cell)
            node.flags.insert(i, flags)
            node.children.insert(i, 0)
            if self._fits(node):
                self._write_node(page, node)
                return None
            return self._split(page, node)
        promoted = self._insert(node.children[i], key, cell, flags)
        if promoted is None:
            return None
        mkey, mval, mflags, right = promoted
        node.keys.insert(i, mkey)
        node.values.insert(i, mval)
        node.flags.insert(i, mflags)
        node.children.insert(i + 1, right)
        if self._fits(node):
            self._write_node(page, node)
            return None
        return self._split(page, node)

    def _split(self, page: int, node: Node):
        """Split an over-full node at its median key. The left half
        stays on `page`; the right half goes to a new page; the median
        key/value move up to the parent. Returns
        (median_key, median_value, median_flags, right_page)."""
        m = len(node.keys) // 2
        mkey, mval, mflags = node.keys[m], node.values[m], node.flags[m]
        right = Node(leaf=node.leaf)
        right.keys = node.keys[m + 1:]
        right.values = node.values[m + 1:]
        right.flags = node.flags[m + 1:]
        right.children = node.children[m + 1:]
        node.keys = node.keys[:m]
        node.values = node.values[:m]
        node.flags = node.flags[:m]
        node.children = node.children[: m + 1]
        right_page = self.pager.alloc_page()
        self._write_node(page, node)
        self._write_node(right_page, right)
        return mkey, mval, mflags, right_page

    # -- delete (tombstone) -------------------------------------------------------
    def delete(self, key) -> bool:
        found = self._find(self.root, key)
        if found is None:
            return False
        page, node, i = found
        if node.flags[i] & CELL_FLAG_TOMBSTONE:
            return False
        self._free_value(node.values[i], node.flags[i])
        node.values[i] = b""
        node.flags[i] = CELL_FLAG_TOMBSTONE
        self._write_node(page, node)
        return True

    # -- scans -----------------------------------------------------------------------
    def scan(self):
        """All live (key, value) pairs in key order."""
        yield from self.scan_range(MINUS_INF, PLUS_INF)

    def scan_range(self, lo, hi, lo_inc: bool = True, hi_inc: bool = True):
        """Live pairs with lo <= key <= hi, in key order. The walk
        prunes whole subtrees that cannot contain matching keys."""
        yield from self._scan_range_node(self.root, lo, hi, lo_inc, hi_inc)

    def _scan_range_node(self, page, lo, hi, lo_inc, hi_inc):
        node = self._read_node(page)
        n = len(node.keys)
        for i in range(n):
            k = node.keys[i]
            ck_lo = _cmp(k, lo)
            ck_hi = _cmp(k, hi)
            if ck_lo < 0 or (ck_lo == 0 and not lo_inc):
                # This key (and its left subtree) are below the range.
                continue
            if ck_hi > 0 or (ck_hi == 0 and not hi_inc):
                # Past the range: the left subtree may still hold
                # keys <= hi, everything after is beyond it.
                if not node.leaf:
                    yield from self._scan_range_node(
                        node.children[i], lo, hi, lo_inc, hi_inc)
                return
            if not node.leaf:
                yield from self._scan_range_node(
                    node.children[i], lo, hi, lo_inc, hi_inc)
            if not (node.flags[i] & CELL_FLAG_TOMBSTONE):
                yield k, self._load_value(node.values[i], node.flags[i])
        if not node.leaf:
            yield from self._scan_range_node(
                node.children[n], lo, hi, lo_inc, hi_inc)

    def range_search(self, op: str, key):
        """Values whose key satisfies `op key` (kept for parity with
        the old in-memory B-tree's API)."""
        if op == "=":
            v = self.search(key)
            return [] if v is None else [v]
        if op == ">":
            return [v for _, v in self.scan_range(key, PLUS_INF,
                                                  lo_inc=False)]
        if op == ">=":
            return [v for _, v in self.scan_range(key, PLUS_INF)]
        if op == "<":
            return [v for _, v in self.scan_range(MINUS_INF, key,
                                                  hi_inc=False)]
        if op == "<=":
            return [v for _, v in self.scan_range(MINUS_INF, key)]
        if op == "!=":
            return [v for _, v in self.scan()
                    if _cmp(_, key) != 0]
        raise ValueError(f"unsupported operator {op!r}")

    # -- inspection ------------------------------------------------------------
    def height(self) -> int:
        h, page = 1, self.root
        node = self._read_node(page)
        while not node.leaf:
            h += 1
            node = self._read_node(node.children[0])
        return h

    def size(self) -> int:
        return sum(1 for _ in self.scan())

    def items(self):
        yield from self.scan()

    # -- teardown -----------------------------------------------------------------
    def free_tree(self, page: int | None = None) -> None:
        """Free every page of this tree (DROP TABLE / DROP INDEX),
        including overflow chains."""
        page = self.root if page is None else page
        node = self._read_node(page)
        for i, cell in enumerate(node.values):
            self._free_value(cell, node.flags[i])
        if not node.leaf:
            for child in node.children:
                if child:
                    self.free_tree(child)
        self.pager.free_page(page)
