"""Omini plugin for UniFi Network (entrypoint run by the Omini core)."""

from omini_sdk import plugin

from omini_unifi.collect import apply, collect, test

plugin.collect(collect)
plugin.test(test)
plugin.apply(apply)

if __name__ == "__main__":
    plugin.run()
