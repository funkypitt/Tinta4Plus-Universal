# T-CON USB protocol notes

Notes on how the Lenovo Windows software talks to the eInk T-CON (USB `048d:8957`,
endpoints OUT `0x02` / IN `0x81`) when it uploads a full-screen image.

**Status: read from captures, not tested on hardware from Linux.** Nothing in this
file is used by the code yet. The only T-CON traffic Tinta4PlusU sends today is the
fixed 36-byte mailbox sequences in `EInkUSBController.py`.

## Source

Two USBPcap captures of the Windows app uploading an image, shipped in a third-party
fork of this project (`tcon-capture.pcap`, `capture2`). They are not stored in this
repository. Everything under "Observed" below was re-derived by parsing those two
files; the fork's own write-up of them contains two misreadings, listed at the end.

## Observed

### Transport

USB Bulk-Only Transport: 31-byte CBW (`USBC`), optional data phase, 13-byte CSW.
The 16-byte command block always starts with `FE 00` and carries the opcode at byte 6.

| Opcode | Direction | Command block as captured | Data phase |
|--------|-----------|---------------------------|------------|
| `0xA8` | OUT | `FE 00 00 00 00 00 A8 00 00 00 00 00 00 00 00 00` | 36-byte mailbox payload |
| `0xA9` | IN  | `FE 00 00 00 00 00 A9 00 00 04 00 00 00 00 00 00` | 4 bytes, `03 00 00 00` every time |
| `0x83` | IN  | `FE 00 18 00 13 C0 83 00 04 00 00 00 00 00 00 00` | 4 bytes |
| `0xA5` | OUT | `FE 00 <addr, 4 bytes big-endian> A5 <len, 2 bytes big-endian> 00 ...` | `len` bytes of pixel data |

`0xA8` with the all-zero command block above is what `CBW_TEMPLATE` already sends.
Data-IN transfers (`bmCBWFlags = 0x80`) work for `0xA9` and `0x83`.

### Memory write (`0xA5`)

- Bytes 2-5 of the command block are a plain 32-bit address. It starts at
  `0x0237F5D0` and advances by exactly the chunk length (`0xF000`) on every chunk.
- 67 chunks: 66 × 61,440 bytes and one of 40,960 bytes, 4,096,000 bytes in total,
  which is 2560 × 1600 at one byte per pixel.
- The whole write phase takes 0.33-0.40 s (5-6 ms per chunk).
- A `0x83` read of `0x180013C0` is interleaved after every 7 chunks. The first one
  in both captures returns `50 00 45 89`.

### Two image buffers

The mailbox payload starting with `0x34` carries width, height and a buffer address:

```
34 00 xx xx 00 00 00 00 | 00 0A | 40 06 | 05 00 | D0 F5 37 02 | 00 ... | 40 06 | ...
                          2560    1600            0x0237F5D0             1600
```

The same `0x34` command is the fifth payload of our `ENABLE_EINK` sequence, with
address `0x027675D0`. The difference between the two addresses is `0x3E8000`, exactly
one 4,096,000-byte frame. So the T-CON has two consecutive frame buffers, and the
upload capture writes the first while our enable sequence points at the second.

### Command order

Capture 1:

1. `A8` payload `60 ...`, then `A9` (twice, with two different `60` payloads)
2. `83` status read
3. 67 × `A5`, with an `83` read after every 7 chunks
4. `A8` payload `34 ...` (buffer address, 2560 × 1600)
5. 124 × `83` status reads
6. `A8` `60 ...` + `A9`, twice
7. `A8` payload `94 00 xx xx 01 00 00 00 00 00 00 00 03 FF 00 ...`

Capture 2 has a single `60`/`A9` pair before the chunks and ends at step 5; it has no
`94` payload.

### Mailbox payload filler bytes

Bytes 2-3 of every mailbox payload, and several 4- and 8-byte groups further in,
change from one Windows session to the next (`BC 28` / `57 00` here, `5B 57` and
`9B BE` in our own sequences) and look like pointers from the Windows process
(`... F8 7F 00 00`). They are most likely uninitialised memory that the T-CON ignores.
This is an inference: no test has sent a payload with those bytes changed.

## Not known

- Whether the sequence above displays an image when sent from Linux.
- What the `0x60`, `0x34` and `0x94` mailbox commands do individually. The fork's
  notes map them to `ITEEnableHWWriting` / `ITESetTconDefaultImageAPI` in Lenovo's
  `EInkTcon.dll`; that mapping was not checked here.
- What the value read from `0x180013C0` means, and what the 124 reads after `0x34`
  wait for.
- How colour is encoded. The data is one byte per pixel and the Lenovo application
  ships a `CFA_mapping.dll`, so colour images probably need a colour-filter-array
  mapping step before upload.

## Misreadings to avoid

- **"D0 flag plus chunk counter" in the `0xA5` command block.** `D0` is the low byte
  of the address `0x0237F5D0`, and the "counter" bytes are the address advancing by
  `0xF000`. Writing chunks to address 0 is not what Windows does.
- **"Zero-length packets between CBW, data and CSW."** USBPcap logs a completion
  record with no payload for every OUT transfer. The captures contain 285 and 299
  zero-length OUT records, which equals the number of CBWs plus OUT data phases in
  each (212 + 73 and 230 + 69). There is no evidence of real zero-length packets on
  the wire, so none should be sent.
- **USB resets and preemptive `CLEAR_FEATURE(ENDPOINT_HALT)`.** Neither appears in the
  captures. A device reset makes the T-CON show its boot splash.
