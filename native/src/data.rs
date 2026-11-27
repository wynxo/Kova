//! The extensions catalog as the server reports it, with Kova's bundled index as the offline copy.

use serde::Deserialize;

/// The index that ships with Kova, so the Marketplace can be browsed before the server answers.
const BUNDLED: &str = include_str!("../../src/kova/catalog_data/index.json");

#[derive(Clone, Debug, Default, Deserialize)]
pub struct Source {
    #[serde(default)]
    pub url: String,
    #[serde(default)]
    pub sha256: String,
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct Entry {
    pub name: String,
    pub kind: String,
    pub version: String,
    #[serde(default)]
    pub description: String,
    #[serde(default)]
    pub author: String,
    #[serde(default)]
    pub homepage: String,
    #[serde(default)]
    pub source: Source,
    #[serde(default)]
    pub permissions: Vec<String>,
    #[serde(default)]
    pub tags: Vec<String>,
    #[serde(default = "available")]
    pub status: String,
    #[serde(default)]
    pub installed_version: Option<String>,
    #[serde(default)]
    pub update_available: bool,
}

fn available() -> String {
    "available".into()
}

impl Entry {
    pub fn available(&self) -> bool {
        self.status == "available"
    }

    /// A display name: "morning-briefing" → "Morning briefing".
    pub fn title(&self) -> String {
        let words = self.name.replace(['-', '_'], " ");
        let mut chars = words.chars();
        match chars.next() {
            Some(first) => first.to_uppercase().chain(chars).collect(),
            None => String::new(),
        }
    }

    pub fn kind_label(&self) -> &'static str {
        match self.kind.as_str() {
            "bot" => "Bot",
            "plugin" => "Plugin",
            "skill" => "Skill",
            _ => "Extension",
        }
    }

    pub fn state_label(&self) -> Option<&'static str> {
        if !self.available() {
            Some("Coming soon")
        } else if self.update_available {
            Some("Update available")
        } else if self.installed_version.is_some() {
            Some("Installed")
        } else {
            None
        }
    }

    pub fn matches(&self, query: &str) -> bool {
        let hay = format!("{} {} {} {} {}", self.name, self.description, self.author, self.kind, self.tags.join(" "))
            .to_lowercase();
        query.to_lowercase().split_whitespace().all(|word| hay.contains(word))
    }
}

#[derive(Deserialize)]
struct Index {
    extensions: Vec<serde_json::Value>,
}

/// Entries from an index or a `/extensions/catalog` reply; ones this app cannot read are skipped.
pub fn parse_entries(values: Vec<serde_json::Value>) -> Vec<Entry> {
    values.into_iter().filter_map(|value| serde_json::from_value(value).ok()).collect()
}

pub fn bundled() -> Vec<Entry> {
    serde_json::from_str::<Index>(BUNDLED).map(|index| parse_entries(index.extensions)).unwrap_or_default()
}
