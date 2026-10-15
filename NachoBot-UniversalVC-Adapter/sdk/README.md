# UniversalVC public client

`nachobot-universalvc-client` installs the standalone `universalvc_client`
module. Its only third-party dependency is `aiohttp`; importing it does not
load the UniversalVC service, native audio devices, or model libraries.

The module is the frozen public SDK copy identified by
`universalvc_client.provenance.txt`. Migration preserves its source bytes and
recorded SHA-256. Changes to the service's internal client do not silently
change this public client; update the copy and provenance deliberately when
revising the public contract.

QQCall consumes this package through its own SDK. There is no SDK copy or
compatibility import in `ncnk_message`.
