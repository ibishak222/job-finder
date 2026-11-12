from .ats import AshbySource, GreenhouseSource, LeverSource
from .boards import ArbeitnowSource, RemoteOKSource, RemotiveSource
from .feeds import RSSSource
from .gov import TeachingVacanciesSource
from .jsonld import JsonLdSource
from .more import (HimalayasSource, JobicySource, RecruiteeSource, SmartRecruitersSource, WeWorkRemotelySource,
                   WorkableSource)
from .nations import HSCNIJobsSource, NHSScotlandSource
from .nhs import NHSJobsSource
from .uk_apis import AdzunaSource, ReedSource

SOURCES = {cls.name: cls for cls in (
    GreenhouseSource, LeverSource, AshbySource, WorkableSource, SmartRecruitersSource, RecruiteeSource,
    RemotiveSource, ArbeitnowSource, RemoteOKSource, HimalayasSource, JobicySource, WeWorkRemotelySource,
    JsonLdSource, RSSSource, ReedSource, AdzunaSource, NHSJobsSource, NHSScotlandSource, HSCNIJobsSource,
    TeachingVacanciesSource,
)}

__all__ = ["SOURCES"]
