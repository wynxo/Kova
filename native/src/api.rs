//! A small blocking client for the local Kova server (`kova serve`, loopback only).
//!
//! It reads the same files the CLI does: the bearer token in `KOVA_HOME/token` and the port in
//! `KOVA_HOME/config.toml` (7700 by default). Calls run on worker threads, never on the UI thread.

use std::path::PathBuf;
use std::time::Duration;

use serde_json::Value;

use crate::data::{Entry, parse_entries};

#[derive(Clone, Debug)]
pub struct Api {
    base: String,
    token: String,
}

fn home() -> Option<PathBuf> {
    if let Some(dir) = std::env::var_os("KOVA_HOME") {
        return Some(PathBuf::from(dir));
    }
    let user = std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE"))?;
    Some(PathBuf::from(user).join(".kova"))
}

/// `port = N` at the top level of config.toml, before any `[section]`.
fn port(config: &str) -> u16 {
    for line in config.lines() {
        let line = line.trim();
        if line.starts_with('[') {
            break;
        }
        if let Some(rest) = line.strip_prefix("port") {
            if let Some(value) = rest.trim().strip_prefix('=') {
                if let Ok(port) = value.trim().parse() {
                    return port;
                }
            }
        }
    }
    7700
}

impl Api {
    /// The server this user's Kova runs, or `None` before Kova has made a token.
    pub fn discover() -> Option<Api> {
        let home = home()?;
        let token = std::fs::read_to_string(home.join("token")).ok()?.trim().to_owned();
        if token.is_empty() {
            return None;
        }
        let config = std::fs::read_to_string(home.join("config.toml")).unwrap_or_default();
        Some(Api { base: format!("http://127.0.0.1:{}", port(&config)), token })
    }

    fn agent() -> ureq::Agent {
        ureq::Agent::config_builder()
            .timeout_global(Some(Duration::from_secs(60)))
            .http_status_as_error(false)
            .build()
            .into()
    }

    fn read(mut response: ureq::http::Response<ureq::Body>) -> Result<Value, String> {
        let status = response.status().as_u16();
        let body = response.body_mut().read_to_string().map_err(|e| e.to_string())?;
        let value: Value = serde_json::from_str(&body).unwrap_or(Value::Null);
        if (200..300).contains(&status) {
            return Ok(value);
        }
        // FastAPI puts the readable reason in `detail`.
        Err(value.get("detail").and_then(Value::as_str).map(str::to_owned)
            .unwrap_or_else(|| format!("Kova answered HTTP {status}.")))
    }

    fn get(&self, path: &str) -> Result<Value, String> {
        let response = Self::agent().get(&format!("{}{path}", self.base))
            .header("Authorization", &format!("Bearer {}", self.token))
            .call()
            .map_err(|_| "Kova isn't running. Start it with: kova serve".to_string())?;
        Self::read(response)
    }

    fn post(&self, path: &str, body: &Value) -> Result<Value, String> {
        let response = Self::agent().post(&format!("{}{path}", self.base))
            .header("Authorization", &format!("Bearer {}", self.token))
            .header("Content-Type", "application/json")
            .send(body.to_string())
            .map_err(|_| "Kova isn't running. Start it with: kova serve".to_string())?;
        Self::read(response)
    }

    pub fn catalog(&self) -> Result<Vec<Entry>, String> {
        let value = self.get("/extensions/catalog")?;
        let entries = value.get("entries").and_then(Value::as_array).cloned().unwrap_or_default();
        Ok(parse_entries(entries))
    }

    /// Download and verify one extension; returns what it adds and a one-use ticket.
    pub fn preview(&self, name: &str, version: &str) -> Result<Value, String> {
        self.post(&format!("/extensions/catalog/{}/preview", encode(name)), &serde_json::json!({"version": version}))
    }

    /// Drop a verified download the user did not confirm.
    pub fn cancel(&self, ticket: &str) -> Result<(), String> {
        let response = Self::agent().delete(&format!("{}/extensions/catalog/preview/{}", self.base, encode(ticket)))
            .header("Authorization", &format!("Bearer {}", self.token))
            .call()
            .map_err(|e| e.to_string())?;
        Self::read(response).map(|_| ())
    }

    pub fn install(&self, ticket: &str, upgrade: bool) -> Result<String, String> {
        let value = self.post("/extensions/install", &serde_json::json!({"ticket": ticket, "upgrade": upgrade}))?;
        Ok(value.get("installed").and_then(Value::as_str).unwrap_or("").to_owned())
    }
}

impl Api {
    pub fn images(&self) -> Result<Value, String> {
        self.get("/images")
    }

    pub fn gallery(&self) -> Result<Vec<Value>, String> {
        Ok(self.get("/images/gallery")?.as_array().cloned().unwrap_or_default())
    }

    /// One saved picture's PNG bytes.
    pub fn picture(&self, name: &str) -> Result<Vec<u8>, String> {
        let mut response = Self::agent().get(&format!("{}/images/files/{}", self.base, encode(name)))
            .header("Authorization", &format!("Bearer {}", self.token))
            .call()
            .map_err(|e| e.to_string())?;
        if response.status().as_u16() != 200 {
            return Err(format!("Kova answered HTTP {}.", response.status().as_u16()));
        }
        response.body_mut().with_config().limit(64 * 1024 * 1024).read_to_vec().map_err(|e| e.to_string())
    }

    pub fn generate(&self, prompt: &str, size: Option<&str>) -> Result<Value, String> {
        let mut body = serde_json::json!({"prompt": prompt});
        if let Some(size) = size {
            body["size"] = Value::from(size);
        }
        self.post("/images/generate", &body)
    }

    pub fn choose(&self, model: &str) -> Result<(), String> {
        let response = Self::agent().put(&format!("{}/images/active", self.base))
            .header("Authorization", &format!("Bearer {}", self.token))
            .header("Content-Type", "application/json")
            .send(serde_json::json!({"model": model}).to_string())
            .map_err(|e| e.to_string())?;
        Self::read(response).map(|_| ())
    }

    /// Download a model, calling `progress(completed, total)` for each event; ends at `done` or the
    /// first `error`.
    pub fn install_model(&self, model: &str, mut progress: impl FnMut(u64, u64)) -> Result<(), String> {
        use std::io::BufRead;
        let mut response = ureq::Agent::config_builder().timeout_global(None).http_status_as_error(false).build()
            .new_agent()
            .post(&format!("{}/images/models/{}/install", self.base, encode(model)))
            .header("Authorization", &format!("Bearer {}", self.token))
            .send_empty()
            .map_err(|_| "Kova isn't running. Start it with: kova serve".to_string())?;
        if response.status().as_u16() != 200 {
            return Self::read(response).map(|_| ());
        }
        let reader = std::io::BufReader::new(response.body_mut().with_config().limit(u64::MAX).reader());
        for line in reader.lines() {
            let line = line.map_err(|e| e.to_string())?;
            let Some(data) = line.strip_prefix("data: ") else { continue };
            let event: Value = serde_json::from_str(data).unwrap_or(Value::Null);
            match event.get("type").and_then(Value::as_str) {
                Some("progress") => progress(
                    event.get("completed").and_then(Value::as_u64).unwrap_or(0),
                    event.get("total").and_then(Value::as_u64).unwrap_or(0)),
                Some("error") => return Err(event.get("message").and_then(Value::as_str).unwrap_or("Download failed.").to_owned()),
                Some("done") => return Ok(()),
                _ => {}
            }
        }
        Err("The download stopped before it finished. Try again: it resumes.".into())
    }
}

/// Catalog names are already a safe slug; this only guards the path against anything else.
fn encode(name: &str) -> String {
    name.chars().filter(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.')).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn port_comes_from_the_top_level_only() {
        assert_eq!(port("port = 7811\n[catalog]\nport = 1\n"), 7811);
        assert_eq!(port("[local]\nport = 9\n"), 7700);
        assert_eq!(port(""), 7700);
    }

    #[test]
    fn names_cannot_leave_their_path_segment() {
        assert_eq!(encode("../../token"), "....token");
        assert_eq!(encode("morning-briefing"), "morning-briefing");
    }
}
