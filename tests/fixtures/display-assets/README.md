# Display catalog fixture

`catalog.json` is a snapshot of the standard catalog shipped by `palmimo-image`
at `/usr/local/share/palmimo/display-assets/catalog.json`. It is shared by SDK
tests and the docs site's browser runtime tests; it is not the runtime catalog.

When updating the snapshot, copy that file from the target image, preserving its
schema, IDs, descriptions, tags, and parameter metadata. Review the diff against
the image version being tested, then run the SDK and docs-site test commands in
[CONTRIBUTING.md](../../../CONTRIBUTING.md) and the
[docs site README](../../../docs-site/README.md#commands). Update the browser
stage's supported assets if the standard vocabulary changes.
