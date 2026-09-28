# scrcpy-server v4.1

`scrcpy-server-v4.1` is the unmodified device-side server from scrcpy 4.1
(https://github.com/Genymobile/scrcpy, Apache-2.0). `stream/scrcpy_session.py`
pushes it to the emulator and speaks its video protocol directly, so the
version is pinned: the protocol changes between scrcpy releases, and a
`brew upgrade scrcpy` must not break the live stream.

- Version: 4.1
- Size: 733706 bytes
- sha256: deacb991ed2509715160ffdc7907e47b4160eb30d1566217e9047fd5b8850cae

To upgrade: replace the file, update `SCRCPY_VERSION` in
`stream/scrcpy_session.py`, and re-verify the header layout documented there
against a real device.
