//! Streaming NDJSON. `NdjsonReader` yields each line with its byte offset so
//! pass two can seek straight back to it; `LineAccess` does that seek.

use std::fs::File;
use std::io::{BufRead, BufReader, Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};

use crate::error::{KilnError, Result};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Line {
    pub number: usize,
    pub offset: u64,
    pub len: usize,
    pub text: String,
}

#[derive(Debug)]
pub struct NdjsonReader {
    path: PathBuf,
    reader: BufReader<File>,
    offset: u64,
    number: usize,
}

impl NdjsonReader {
    /// Opens and rejects a pretty-printed document (first non-blank line is a
    /// lone `{` or `[`) before yielding anything, so the operator gets one
    /// clear message instead of an error per line.
    pub fn open(path: &Path) -> Result<Self> {
        let file = File::open(path).map_err(|e| KilnError::io(path, e))?;
        let mut reader = BufReader::with_capacity(1 << 20, file);
        let mut first = String::new();
        loop {
            first.clear();
            let n = reader
                .read_line(&mut first)
                .map_err(|e| KilnError::io(path, e))?;
            if n == 0 || !first.trim().is_empty() {
                break;
            }
        }
        let head = first.trim();
        if head == "{" || head == "[" {
            return Err(KilnError::Usage(format!(
                "{}: looks like pretty-printed JSON, not NDJSON (one resource per line)",
                path.display()
            )));
        }
        reader
            .seek(SeekFrom::Start(0))
            .map_err(|e| KilnError::io(path, e))?;
        Ok(Self {
            path: path.to_path_buf(),
            reader,
            offset: 0,
            number: 0,
        })
    }
}

impl Iterator for NdjsonReader {
    type Item = Result<Line>;

    fn next(&mut self) -> Option<Self::Item> {
        let mut buf = Vec::new();
        loop {
            buf.clear();
            let start = self.offset;
            let n = match self.reader.read_until(b'\n', &mut buf) {
                Ok(n) => n,
                Err(e) => return Some(Err(KilnError::io(&self.path, e))),
            };
            if n == 0 {
                return None;
            }
            self.offset += n as u64;
            self.number += 1;
            let trimmed_end = buf
                .iter()
                .rposition(|b| !b.is_ascii_whitespace())
                .map_or(0, |p| p + 1);
            let leading = buf[..trimmed_end]
                .iter()
                .position(|b| !b.is_ascii_whitespace());
            let Some(leading) = leading else {
                continue; // blank line
            };
            let slice = &buf[leading..trimmed_end];
            let text = match std::str::from_utf8(slice) {
                Ok(s) => s.to_string(),
                Err(e) => {
                    return Some(Err(KilnError::Usage(format!(
                        "{}: line {}: {e}",
                        self.path.display(),
                        self.number
                    ))))
                }
            };
            if !text.starts_with('{') {
                return Some(Err(KilnError::Usage(format!(
                    "{}: line {}: not a JSON object",
                    self.path.display(),
                    self.number
                ))));
            }
            return Some(Ok(Line {
                number: self.number,
                offset: start + leading as u64,
                len: slice.len(),
                text,
            }));
        }
    }
}

pub struct LineAccess {
    path: PathBuf,
    file: File,
}

impl LineAccess {
    pub fn open(path: &Path) -> Result<Self> {
        let file = File::open(path).map_err(|e| KilnError::io(path, e))?;
        Ok(Self {
            path: path.to_path_buf(),
            file,
        })
    }

    pub fn read_at(&mut self, offset: u64, len: usize) -> Result<String> {
        self.file
            .seek(SeekFrom::Start(offset))
            .map_err(|e| KilnError::io(&self.path, e))?;
        let mut buf = vec![0u8; len];
        self.file
            .read_exact(&mut buf)
            .map_err(|e| KilnError::io(&self.path, e))?;
        String::from_utf8(buf)
            .map_err(|e| KilnError::Usage(format!("{}: offset {offset}: {e}", self.path.display())))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn tmp(content: &str) -> tempfile::NamedTempFile {
        let mut f = tempfile::NamedTempFile::new().unwrap();
        f.write_all(content.as_bytes()).unwrap();
        f
    }

    #[test]
    fn yields_each_nonblank_line_with_its_offset() {
        let f = tmp("{\"id\":\"a\"}\n\n{\"id\":\"b\"}\n");
        let lines: Vec<Line> = NdjsonReader::open(f.path())
            .unwrap()
            .map(|l| l.unwrap())
            .collect();
        assert_eq!(lines.len(), 2);
        assert_eq!(lines[0].offset, 0);
        assert_eq!(lines[0].text, "{\"id\":\"a\"}");
        // Byte layout: `{"id":"a"}` (0..10) + `\n` (10) + blank line's own
        // `\n` (11) + `{"id":"b"}` starting at 12 + trailing `\n` (22).
        assert_eq!(lines[1].offset, 12);
        assert_eq!(lines[1].len, 10);
    }

    #[test]
    fn read_at_returns_the_same_bytes() {
        let f = tmp("{\"id\":\"a\"}\n{\"id\":\"b\"}\n");
        let lines: Vec<Line> = NdjsonReader::open(f.path())
            .unwrap()
            .map(|l| l.unwrap())
            .collect();
        let mut random = LineAccess::open(f.path()).unwrap();
        assert_eq!(
            random.read_at(lines[1].offset, lines[1].len).unwrap(),
            "{\"id\":\"b\"}"
        );
        assert_eq!(
            random.read_at(lines[0].offset, lines[0].len).unwrap(),
            "{\"id\":\"a\"}"
        );
    }

    #[test]
    fn pretty_printed_json_is_rejected_up_front() {
        let f = tmp("{\n  \"resourceType\": \"Bundle\"\n}\n");
        let err = NdjsonReader::open(f.path()).unwrap_err();
        assert!(err.to_string().contains("pretty-printed"), "{err}");
    }

    #[test]
    fn unparseable_line_is_an_error_with_line_number() {
        let f = tmp("{\"id\":\"a\"}\nnot json\n");
        let mut reader = NdjsonReader::open(f.path()).unwrap();
        reader.next().unwrap().unwrap();
        let err = reader.next().unwrap().unwrap_err();
        assert!(err.to_string().contains("line 2"), "{err}");
    }
}
