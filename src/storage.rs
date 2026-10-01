use crate::config::StorageRootConfig;
use anyhow::{Context, Result};
use std::path::{Path, PathBuf};

#[derive(Clone)]
pub struct Storage {
    roots: Vec<StorageRootConfig>,
}

impl Storage {
    pub fn new(roots: Vec<StorageRootConfig>) -> Self {
        Self { roots }
    }

    pub fn roots(&self) -> &[StorageRootConfig] {
        &self.roots
    }

    pub fn root(&self, id: &str) -> Result<&StorageRootConfig> {
        self.roots
            .iter()
            .find(|root| root.id == id)
            .context("storage root not found")
    }

    pub fn resolve_existing(&self, root_id: &str, relative: &str) -> Result<PathBuf> {
        let root = self.root(root_id)?;
        let path = self.resolve(root, relative)?;
        anyhow::ensure!(path.is_file(), "input is not a file");
        Ok(path)
    }

    pub fn resolve_output(&self, root_id: &str, relative: &str) -> Result<PathBuf> {
        let root = self.root(root_id)?;
        anyhow::ensure!(!root.read_only, "storage root is read-only");
        let path = self.resolve(root, relative)?;
        let parent = path.parent().context("output has no parent")?;
        anyhow::ensure!(
            parent.exists() && parent.is_dir(),
            "output directory does not exist"
        );
        anyhow::ensure!(path.file_name().is_some(), "output filename is required");
        Ok(path)
    }

    pub fn list_dir(&self, root_id: &str, relative: &str) -> Result<Vec<DirectoryEntry>> {
        let root = self.root(root_id)?;
        let path = self.resolve(root, relative)?;
        anyhow::ensure!(path.is_dir(), "directory not found");
        let mut entries = std::fs::read_dir(path)
            .context("read storage directory")?
            .filter_map(|entry| entry.ok())
            .filter_map(|entry| {
                let metadata = entry.metadata().ok()?;
                let name = entry.file_name().to_string_lossy().to_string();
                if name.starts_with('.') {
                    return None;
                }
                Some(DirectoryEntry {
                    name,
                    directory: metadata.is_dir(),
                    size: metadata.len(),
                })
            })
            .collect::<Vec<_>>();
        entries.sort_by_key(|entry| (!entry.directory, entry.name.to_lowercase()));
        Ok(entries)
    }

    fn ensure_mounted(root: &StorageRootConfig) -> Result<()> {
        if let Some(marker) = &root.mount_marker {
            anyhow::ensure!(
                root.path.join(marker).is_file(),
                "storage mount marker is missing"
            );
        }
        Ok(())
    }

    fn resolve(&self, root: &StorageRootConfig, relative: &str) -> Result<PathBuf> {
        Self::ensure_mounted(root)?;
        let relative = Path::new(relative);
        anyhow::ensure!(!relative.is_absolute(), "absolute paths are not allowed");
        anyhow::ensure!(
            relative
                .components()
                .all(|component| { matches!(component, std::path::Component::Normal(_)) }),
            "path traversal is not allowed"
        );
        let root_path = std::fs::canonicalize(&root.path)
            .with_context(|| format!("storage root unavailable: {}", root.path.display()))?;
        let candidate = root_path.join(relative);
        if candidate.exists() {
            let canonical =
                std::fs::canonicalize(&candidate).context("canonicalize storage path")?;
            anyhow::ensure!(
                canonical.starts_with(&root_path),
                "path escapes storage root"
            );
            anyhow::ensure!(
                !is_symlink_path(&root_path, relative),
                "symlinks are not allowed"
            );
            Ok(canonical)
        } else {
            let parent = candidate.parent().context("path has no parent")?;
            let canonical_parent =
                std::fs::canonicalize(parent).context("canonicalize output directory")?;
            anyhow::ensure!(
                canonical_parent.starts_with(&root_path),
                "path escapes storage root"
            );
            Ok(canonical_parent.join(candidate.file_name().context("missing filename")?))
        }
    }
}

fn is_symlink_path(root: &Path, relative: &Path) -> bool {
    let mut current = root.to_path_buf();
    for component in relative.components() {
        current.push(component.as_os_str());
        if std::fs::symlink_metadata(&current)
            .is_ok_and(|metadata| metadata.file_type().is_symlink())
        {
            return true;
        }
    }
    false
}

#[derive(serde::Serialize)]
pub struct DirectoryEntry {
    pub name: String,
    pub directory: bool,
    pub size: u64,
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn rejects_parent_traversal() {
        let dir = tempfile::tempdir().unwrap();
        let storage = Storage::new(vec![StorageRootConfig {
            id: "test".into(),
            label: "Test".into(),
            path: dir.path().into(),
            read_only: false,
            mount_marker: None,
        }]);
        assert!(storage.resolve_existing("test", "../secret.mp4").is_err());
    }

    #[test]
    fn lists_files_and_directories() {
        let dir = tempfile::tempdir().unwrap();
        fs::create_dir(dir.path().join("clips")).unwrap();
        fs::write(dir.path().join("clip.mp4"), b"x").unwrap();
        let storage = Storage::new(vec![StorageRootConfig {
            id: "test".into(),
            label: "Test".into(),
            path: dir.path().into(),
            read_only: false,
            mount_marker: None,
        }]);
        let entries = storage.list_dir("test", "").unwrap();
        assert_eq!(entries.len(), 2);
        assert!(entries[0].directory);
    }
}
