if __name__ == "__main__":
    # Default entry point. Will start the enterprise server if installed, if not
    # the community one will be used. Allows running the server using
    # `python -m rasax.community`

    # it would be better to put this at the top level `rasax` package, but that is
    # not possible because of the multi-package build of community and enterprise.
    # the top level package can't contain code :(
    # https://packaging.python.org/guides/packaging-namespace-packages/#native-namespace-packages
    import rasax.community.utils.common as common_utils

    if common_utils.is_enterprise_installed():
        from rasax.enterprise.server import main  # pytype: disable=import-error

        main()
    else:
        from rasax.community.server import main

        main()
