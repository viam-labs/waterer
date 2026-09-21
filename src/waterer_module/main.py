"""Waterer module entrypoint. Registers models and starts the server."""

import asyncio

from viam.components.generic import Generic
from viam.module.module import Module
from viam.resource.registry import Registry, ResourceCreatorRegistration

from .pump import Pump


def _register() -> None:
    Registry.register_resource_creator(
        Generic.API,
        Pump.MODEL,
        ResourceCreatorRegistration(Pump.new, Pump.validate_config),
    )


async def main() -> None:
    _register()
    module = Module.from_args()
    module.add_model_from_registry(Generic.API, Pump.MODEL)
    await module.start()


if __name__ == "__main__":
    asyncio.run(main())
