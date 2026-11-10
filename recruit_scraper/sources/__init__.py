from .ats import AshbySource, GreenhouseSource, LeverSource
from .boards import ArbeitnowSource, RemoteOKSource, RemotiveSource
from .jsonld import JsonLdSource
from .more import (HimalayasSource, JobicySource, RecruiteeSource, SmartRecruitersSource, WeWorkRemotelySource,
                   WorkableSource)
from .nhs import NHSJobsSource
from .uk_apis import AdzunaSource, ReedSource

SOURCES = {cls.name: cls for cls in (
    GreenhouseSource, LeverSource, AshbySource, WorkableSource, SmartRecruitersSource, RecruiteeSource,
    RemotiveSource, ArbeitnowSource, RemoteOKSource, HimalayasSource, JobicySource, WeWorkRemotelySource,
    JsonLdSource, ReedSource, AdzunaSource, NHSJobsSource,
)}

__all__ = ["SOURCES"]
