"""
Related files finder for code review context.

Identifies and fetches related files (entities, interfaces, etc.) from GitLab
to provide better context for reviews - especially for detecting Anemic Domain Model.
"""

import logging
import re

from .gitlab_client import GitLabClient

logger = logging.getLogger(__name__)


def extract_java_imports(file_content: str) -> list[tuple[str, str]]:
    """
    Extract import statements from Java file.

    Returns list of tuples: (package_path, class_name)
    e.g., "import com.example.domain.Order;" -> ("com.example.domain", "Order")
    """
    imports: list[tuple[str, str]] = []
    pattern = r"^import\s+([\w.]+)\.(\w+);"

    for line in file_content.split("\n"):
        match = re.match(pattern, line.strip())
        if match:
            imports.append((match.group(1), match.group(2)))

    return imports


def guess_java_file_path(package_path: str, class_name: str) -> list[str]:
    """
    Guess possible file paths for a Java class.

    Args:
        package_path: Package like "com.example.domain"
        class_name: Class name like "Order"

    Returns:
        List of possible file paths to try
    """
    # Convert package to path: com.example.domain -> com/example/domain
    package_dir = package_path.replace(".", "/")

    # Common source roots
    source_roots = [
        "src/main/java",
        "src/java",
        "app/src/main/java",
    ]

    paths = []
    for root in source_roots:
        paths.append(f"{root}/{package_dir}/{class_name}.java")

    return paths


def is_likely_entity(class_name: str, package_path: str) -> bool:
    """
    Check if a class is likely a domain entity based on naming conventions.
    """
    # Package hints
    entity_packages = ["domain", "entity", "model", "aggregate"]
    if any(pkg in package_path.lower() for pkg in entity_packages):
        return True

    # Exclude common non-entities
    exclude_suffixes = [
        "Service",
        "Repository",
        "Controller",
        "Dto",
        "DTO",
        "Exception",
        "Config",
        "Configuration",
        "Mapper",
        "Factory",
        "Builder",
        "Utils",
        "Util",
        "Helper",
        "Request",
        "Response",
        "Validator",
        "Converter",
        "Listener",
        "Handler",
        "Processor",
        "Client",
    ]
    if any(class_name.endswith(suffix) for suffix in exclude_suffixes):
        return False

    # Exclude Java standard library
    return not package_path.startswith(("java.", "javax.", "org.springframework", "lombok"))


def is_likely_value_object(class_name: str, package_path: str) -> bool:
    """
    Check if a class is likely a value object.
    """
    value_object_packages = ["vo", "valueobject", "value"]
    if any(pkg in package_path.lower() for pkg in value_object_packages):
        return True

    # Common value object naming
    value_object_names = ["Money", "Email", "Address", "PhoneNumber", "DateRange", "Period"]
    return class_name in value_object_names


def fetch_related_entities(
    gitlab: GitLabClient,
    project: str,
    branch: str,
    changed_files_content: dict[str, str],
    max_files: int = 5,
    max_lines: int = 300,
) -> dict[str, str]:
    """
    Fetch entity/domain files related to the changed files.

    This helps with Anemic Domain Model detection by providing
    the entity classes that services are working with.

    Args:
        gitlab: GitLab client
        project: Project path
        branch: Branch to fetch from (usually MR source branch)
        changed_files_content: Dict of changed file paths to their content
        max_files: Maximum entity files to fetch
        max_lines: Maximum lines per file

    Returns:
        Dict mapping file paths to content
    """
    related: dict[str, str] = {}
    seen_classes: set[str] = set()

    for file_path, content in changed_files_content.items():
        if not file_path.endswith(".java"):
            continue

        imports = extract_java_imports(content)

        for package_path, class_name in imports:
            if class_name in seen_classes:
                continue
            if len(related) >= max_files:
                break

            # Check if this looks like an entity or value object
            if not (
                is_likely_entity(class_name, package_path)
                or is_likely_value_object(class_name, package_path)
            ):
                continue

            seen_classes.add(class_name)

            # Try to fetch the file
            possible_paths = guess_java_file_path(package_path, class_name)

            for try_path in possible_paths:
                try:
                    entity_content = gitlab.get_file_content(project, try_path, branch)
                    # Verify it's an entity (has @Entity or domain logic)
                    if entity_content and (
                        "@Entity" in entity_content or "domain" in try_path.lower()
                    ):
                        # Truncate if needed
                        lines = entity_content.split("\n")
                        if len(lines) > max_lines:
                            entity_content = "\n".join(lines[:max_lines])
                            entity_content += (
                                f"\n\n// ... truncated ({len(lines) - max_lines} more lines)"
                            )

                        related[try_path] = entity_content
                        logger.info(f"Found related entity: {try_path}")
                        break
                except Exception as e:
                    logger.debug(f"Could not fetch {try_path}: {e}")

    return related


def fetch_related_interfaces(
    gitlab: GitLabClient,
    project: str,
    branch: str,
    changed_files_content: dict[str, str],
    max_files: int = 3,
    max_lines: int = 200,
) -> dict[str, str]:
    """
    Fetch interfaces that changed classes implement.

    Helps verify implementations match contracts.
    """
    related: dict[str, str] = {}

    for file_path, content in changed_files_content.items():
        if not file_path.endswith(".java"):
            continue

        # Find "implements X" or "extends X"
        implements_pattern = r"(?:implements|extends)\s+([\w,\s]+?)(?:\s*\{|implements)"
        match = re.search(implements_pattern, content)

        if match:
            interfaces = [i.strip() for i in match.group(1).split(",")]

            for interface_name in interfaces:
                if len(related) >= max_files:
                    break

                # Skip common base classes
                if interface_name in ["Object", "Serializable", "Comparable"]:
                    continue

                # Try to find and fetch the interface
                imports = extract_java_imports(content)
                for package_path, class_name in imports:
                    if class_name == interface_name:
                        paths = guess_java_file_path(package_path, class_name)
                        for try_path in paths:
                            try:
                                interface_content = gitlab.get_file_content(
                                    project, try_path, branch
                                )
                                if interface_content and "interface" in interface_content:
                                    lines = interface_content.split("\n")
                                    if len(lines) > max_lines:
                                        interface_content = "\n".join(lines[:max_lines])
                                    related[try_path] = interface_content
                                    logger.info(f"Found interface: {try_path}")
                                    break
                            except Exception:
                                pass
                        break

    return related


def get_related_files_for_review(
    gitlab: GitLabClient,
    project: str,
    branch: str,
    changed_files_content: dict[str, str],
    max_total_files: int = 8,
) -> dict[str, str]:
    """
    Get all related files useful for code review.

    Args:
        gitlab: GitLab client
        project: Project path
        branch: Source branch of MR
        changed_files_content: Content of changed files

    Returns:
        Dict of related file paths to content
    """
    related = {}

    # Fetch entities (most important for Anemic Domain Model detection)
    entities = fetch_related_entities(
        gitlab,
        project,
        branch,
        changed_files_content,
        max_files=5,
    )
    related.update(entities)

    # Fetch interfaces if we have room
    remaining = max_total_files - len(related)
    if remaining > 0:
        interfaces = fetch_related_interfaces(
            gitlab,
            project,
            branch,
            changed_files_content,
            max_files=remaining,
        )
        related.update(interfaces)

    if related:
        logger.info(f"Found {len(related)} related files for context")

    return related
