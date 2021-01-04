class GitConcurrentOperationException(Exception):
    """Exception that is raised when there's another Git operation in progress."""


class GitCommitError(Exception):
    """Exception in case an error happens when trying to push changes."""


class CredentialsError(Exception):
    """Exception if the user doesn't have the required read and write permissions for
       the Git repository.
    """


class GitHTTPSCredentialsError(CredentialsError):
    """Exception raised in case an error occurred when trying to fetch or push
    from a remote repository, due to invalid credentials being specified for
    an HTTPS connection."""


class ProjectLayoutError(Exception):
    """Exception if the connected repository doesn't have the required layout.

    Raised if domain, config or data directory don't exist.
    """
