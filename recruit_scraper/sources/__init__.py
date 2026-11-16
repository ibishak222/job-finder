from .ats import AshbySource, GreenhouseSource, LeverSource
from .boards import ArbeitnowSource, RemoteOKSource, RemotiveSource
from .employer_sites import EmployerSitesSource
from .feeds import RSSSource
from .gov import TeachingVacanciesSource
from .jsonld import JsonLdSource
from .more import (HimalayasSource, JobicySource, RecruiteeSource, SmartRecruitersSource, WeWorkRemotelySource,
                   WorkableSource)
from .nations import HSCNIJobsSource, NHSScotlandSource
from .nhs import NHSJobsSource

SOURCES = {cls.name: cls for cls in (
    GreenhouseSource, LeverSource, AshbySource, WorkableSource, SmartRecruitersSource, RecruiteeSource,
    RemotiveSource, ArbeitnowSource, RemoteOKSource, HimalayasSource, JobicySource, WeWorkRemotelySource,
    JsonLdSource, RSSSource, NHSJobsSource, NHSScotlandSource, HSCNIJobsSource,
    TeachingVacanciesSource, EmployerSitesSource,
)}

__all__ = ["SOURCES"]
