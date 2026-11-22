#!/usr/bin/env python3
"""
pager.py — the storage layer of minidb, following the architecture of
mkdb / "Writing My Own Database From Scratch" (Antonio Sarosi):

    * The database is ONE file, a sequence of fixed-size pages
      (PAGE_SIZE = 4096 bytes, one disk block). A page number * 4096 is
      the file offset, so any page is one seek away.
    * Page 0 is a header: magic, page size, page count, the root page
      of the schema B-tree, and the head of the free-page list.
    * B-tree nodes live in *slotted pages*: a slot directory grows
      right from the page start, cell bytes grow left from the page
      end. Shifting a 4-byte slot entry reorders cells instead of
      moving the variable-length cell bytes around.
    * Payloads larger than a quarter of a page spill to *overflow
      pages*: the cell keeps the first chunk and its last 4 bytes
      point at the first overflow page; overflow pages form a linked
      list (next-page pointer in each header).
    * A *clock cache* (second-chance) keeps hot pages in memory:
      every frame has a reference bit and a dirty bit; the clock hand
      clears reference bits and evicts the first frame it finds with
      ref == 0, writing it back first if dirty.
    * Transactions use a *journal file*, exactly like mkdb: before a
      page is modified for the first time inside a transaction, its
      original bytes are appended to the journal. COMMIT flushes the
      dirty pages and deletes the journal; ROLLBACK copies the
      original pages back from the journal. If a journal file is found
      when the database is opened, the transaction never committed, so
      the same restore runs as crash recovery.

Only the Python standard library is used.
"""

from __future__ import annotations

import os
import struct

PAGE_SIZE = 4096
MAGIC = b"MINIDB01"

PAGE_TYPE_BTREE = 1
PAGE_TYPE_OVERFLOW = 2
PAGE_TYPE_FREE = 3

# A payload bigger than a quarter of a page overflows (mkdb's rule).
OVERFLOW_THRESHOLD = PAGE_SIZE // 4

# Page 0 header: magic(8) page_size(u32) page_count(u32)
#                schema_root(u32) free_list_head(u32)  -> 24 bytes.
HEADER = struct.Struct(">8sIIII")

# B-tree node (slotted page) header, 16 bytes:
#   type(1) is_leaf(1) num_cells(u16) cell_start(u16)
#   right_child(u32) reserved(6)
NODE_HEADER = struct.Struct(">BBHHI6s")
NODE_HEADER_SIZE = NODE_HEADER.size  # 16

# One slot directory entry: cell offset(u16), cell length(u16).
SLOT = struct.Struct(">HH")

# One cell: key_len(u16) val_len(u16) flags(u8) child_page(u32),
# then key bytes, then value bytes. flags: 1 = tombstone, 2 = overflow
# (the value's last 4 bytes are the first overflow page number).
CELL_HEADER = struct.Struct(">HHBI")
CELL_FLAG_TOMBSTONE = 0x01
CELL_FLAG_OVERFLOW = 0x02

# Overflow page header, 8 bytes: type(1) pad(1) data_len(u16),
# then PAGE_SIZE - 8 bytes of payload. next_page(u32) links the
# chain; data_len says how many payload bytes this page holds, so a
# partially-filled tail page reads back exactly.
OVERFLOW_HEADER = struct.Struct(">BBHI")

# Free page header, 8 bytes: type(1) pad(3) next_free(u32).
FREE_HEADER = struct.Struct(">B3sI")


class PagerError(Exception):
    pass


class ClockCache:
    """Second-chance (clock) page cache.

    Frames are (page_no, data bytearray, ref bit, dirty bit). A lookup
    sets the reference bit; eviction sweeps the hand, clearing ref
    bits, and takes the first frame with ref == 0, writing it back
    through ``on_evict`` if dirty.
    """

    def __init__(self, capacity: int, on_evict=None):
        if capacity < 1:
            raise ValueError("cache capacity must be >= 1")
        self.capacity = capacity
        self.frames: list = []  # [page_no, bytearray, ref, dirty]
        self.hand = 0
        self.on_evict = on_evict
        self.hits = 0
        self.misses = 0

    def _find(self, page_no: int):
        for i, f in enumerate(self.frames):
            if f[0] == page_no:
                return i
        return -1

    def get(self, page_no: int):
        i = self._find(page_no)
        if i < 0:
            self.misses += 1
            return None
        self.hits += 1
        self.frames[i][2] = 1
        return bytes(self.frames[i][1])

    def put(self, page_no: int, data: bytes, dirty: bool = False) -> None:
        i = self._find(page_no)
        if i >= 0:
            self.frames[i][1] = bytearray(data)
            self.frames[i][2] = 1
            self.frames[i][3] = self.frames[i][3] or dirty
            return
        if len(self.frames) < self.capacity:
            self.frames.append([page_no, bytearray(data), 1, dirty])
            return
        # Evict with the clock hand.
        while True:
            f = self.frames[self.hand]
            if f[2] == 1:
                f[2] = 0
                self.hand = (self.hand + 1) % len(self.frames)
                continue
            if f[3] and self.on_evict is not None:
                self.on_evict(f[0], bytes(f[1]))
            self.frames[self.hand] = [page_no, bytearray(data), 1, dirty]
            self.hand = (self.hand + 1) % len(self.frames)
            return

    def invalidate(self, page_no: int) -> None:
        i = self._find(page_no)
        if i >= 0:
            del self.frames[i]
            if self.hand > i:
                self.hand -= 1
            if self.frames:
                self.hand %= len(self.frames)
            else:
                self.hand = 0

    def invalidate_all(self) -> None:
        self.frames.clear()
        self.hand = 0

    def dirty_pages(self):
        return [(f[0], bytes(f[1])) for f in self.frames if f[3]]

    def __len__(self):
        return len(self.frames)


class Pager:
    """Owns the database file: page reads/writes, allocation, the
    clock cache, and the transaction journal."""

    def __init__(self, path: str, cache_size: int = 128):
        self.path = os.path.abspath(path)
        self.journal_path = self.path + "-journal"
        self.cache = ClockCache(cache_size, on_evict=self._write_to_disk)
        self.in_txn = False
        self._journaled: set[int] = set()   # pages already journaled
        self._txn_new_pages: set[int] = set()  # pages allocated in txn
        self._open_file()
        self._recover()  # crash recovery: a leftover journal rolls back
        self._read_header()

    # -- file / header ------------------------------------------------------
    def _open_file(self) -> None:
        exists = os.path.exists(self.path)
        self._file = open(self.path, "r+b" if exists else "w+b")
        if not exists or os.path.getsize(self.path) == 0:
            self._file.write(b"\x00" * PAGE_SIZE)
            self._file.flush()
            self.page_count = 1
            self.schema_root = 0
            self.free_head = 0
            self._write_header_to_disk()
            self._file.flush()
            os.fsync(self._file.fileno())

    def _read_header(self) -> None:
        self._file.seek(0)
        raw = self._file.read(PAGE_SIZE)
        if len(raw) < HEADER.size:
            raise PagerError("database file too small for a header")
        magic, page_size, count, schema_root, free_head = HEADER.unpack(
            raw[: HEADER.size])
        if magic != MAGIC:
            raise PagerError("not a minidb database file (bad magic)")
        if page_size != PAGE_SIZE:
            raise PagerError(f"unsupported page size {page_size}")
        self.page_count = count
        self.schema_root = schema_root
        self.free_head = free_head

    def _header_bytes(self) -> bytes:
        raw = bytearray(PAGE_SIZE)
        HEADER.pack_into(raw, 0, MAGIC, PAGE_SIZE, self.page_count,
                         self.schema_root, self.free_head)
        return bytes(raw)

    def _write_header_to_disk(self) -> None:
        self._file.seek(0)
        self._file.write(self._header_bytes())

    def _write_header(self) -> None:
        """Write page 0 through the normal path (journaled in a txn)."""
        self.write_page(0, self._header_bytes())

    # -- low-level disk I/O ---------------------------------------------------
    def _read_from_disk(self, page_no: int) -> bytes:
        self._file.seek(page_no * PAGE_SIZE)
        raw = self._file.read(PAGE_SIZE)
        if len(raw) != PAGE_SIZE:
            raise PagerError(f"short read on page {page_no}")
        return raw

    def _write_to_disk(self, page_no: int, data: bytes) -> None:
        self._file.seek(page_no * PAGE_SIZE)
        self._file.write(data)

    # -- page access ----------------------------------------------------------
    def read_page(self, page_no: int) -> bytes:
        if page_no < 0 or page_no >= self.page_count:
            raise PagerError(f"page {page_no} out of range "
                             f"(0..{self.page_count - 1})")
        hit = self.cache.get(page_no)
        if hit is not None:
            return hit
        raw = self._read_from_disk(page_no)
        self.cache.put(page_no, raw)
        return raw

    def write_page(self, page_no: int, data: bytes) -> None:
        if len(data) != PAGE_SIZE:
            raise PagerError(f"page must be exactly {PAGE_SIZE} bytes")
        if page_no < 0 or page_no >= self.page_count:
            raise PagerError(f"page {page_no} out of range")
        if self.in_txn and page_no not in self._journaled \
                and page_no not in self._txn_new_pages:
            self._journal_append(page_no)
            self._journaled.add(page_no)
        self.cache.put(page_no, data, dirty=True)

    # -- allocation -------------------------------------------------------------
    def alloc_page(self) -> int:
        """Take a page from the free list, or grow the file."""
        if self.free_head != 0:
            page_no = self.free_head
            raw = self.read_page(page_no)
            _t, _p, nxt = FREE_HEADER.unpack(raw[: FREE_HEADER.size])
            self.free_head = nxt
            self._write_header()
            return page_no
        page_no = self.page_count
        self.page_count += 1
        self._write_header()  # journals the old header inside a txn
        # Extend the file with a zeroed page (beyond page_count this
        # space is ignored if the txn rolls back).
        self._file.seek(page_no * PAGE_SIZE)
        self._file.write(b"\x00" * PAGE_SIZE)
        self._file.flush()
        if self.in_txn:
            self._txn_new_pages.add(page_no)
        return page_no

    def free_page(self, page_no: int) -> None:
        raw = bytearray(PAGE_SIZE)
        FREE_HEADER.pack_into(raw, 0, PAGE_TYPE_FREE, b"\x00\x00\x00",
                              self.free_head)
        self.free_head = page_no
        self.write_page(page_no, bytes(raw))
        self._write_header()

    # -- journal ---------------------------------------------------------------
    def _journal_append(self, page_no: int) -> None:
        """Copy the page's ORIGINAL bytes to the journal, fsynced, so
        a crash can never leave a half-applied transaction behind."""
        original = self._read_from_disk(page_no)
        with open(self.journal_path, "ab") as jf:
            jf.write(struct.pack(">I", page_no))
            jf.write(original)
            jf.flush()
            os.fsync(jf.fileno())

    def begin(self) -> None:
        if self.in_txn:
            raise PagerError("transaction already in progress")
        self.in_txn = True
        self._journaled = set()
        self._txn_new_pages = set()
        # A fresh (empty) journal marks "transaction in progress".
        open(self.journal_path, "wb").close()

    def commit(self) -> None:
        if not self.in_txn:
            return
        self.flush()
        self.in_txn = False
        self._journaled = set()
        self._txn_new_pages = set()
        if os.path.exists(self.journal_path):
            os.remove(self.journal_path)

    def rollback(self) -> None:
        if not self.in_txn and not os.path.exists(self.journal_path):
            return
        self._restore_journal()
        self.in_txn = False
        self._journaled = set()
        self._txn_new_pages = set()

    def _restore_journal(self) -> None:
        """Copy every journaled page back over the database file."""
        if not os.path.exists(self.journal_path):
            return
        with open(self.journal_path, "rb") as jf:
            while True:
                head = jf.read(4)
                if not head:
                    break
                (page_no,) = struct.unpack(">I", head)
                data = jf.read(PAGE_SIZE)
                if len(data) != PAGE_SIZE:
                    raise PagerError("corrupt journal file")
                self.cache.invalidate(page_no)
                self._write_to_disk(page_no, data)
        self._file.flush()
        os.fsync(self._file.fileno())
        os.remove(self.journal_path)
        self.cache.invalidate_all()
        self._read_header()  # page_count / free list may have reverted

    def _recover(self) -> None:
        """Crash recovery on open: a leftover journal means the last
        transaction never committed, so roll it back."""
        if os.path.exists(self.journal_path):
            self._restore_journal()

    # -- flushing / closing ---------------------------------------------------
    def flush(self) -> None:
        for page_no, data in sorted(self.cache.dirty_pages()):
            self._write_to_disk(page_no, data)
            i = self.cache._find(page_no)
            if i >= 0:
                self.cache.frames[i][3] = False
        self._file.flush()
        os.fsync(self._file.fileno())

    def close(self) -> None:
        if getattr(self, "_closed", False):
            return
        self._closed = True
        try:
            self.flush()
        finally:
            self._file.close()

    # -- diagnostics -----------------------------------------------------------
    def stats(self) -> dict:
        return {
            "page_size": PAGE_SIZE,
            "page_count": self.page_count,
            "free_head": self.free_head,
            "schema_root": self.schema_root,
            "cache_frames": len(self.cache),
            "cache_hits": self.cache.hits,
            "cache_misses": self.cache.misses,
            "in_transaction": self.in_txn,
            "journal_exists": os.path.exists(self.journal_path),
        }
