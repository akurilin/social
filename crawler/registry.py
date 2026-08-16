"""Registry of procedural source adapters."""

from crawler.adapters.astor_center import (
    ADAPTER_ID as ASTOR_CENTER_ADAPTER_ID,
    AstorCenterAdapter,
)
from crawler.adapters.astor_wines import (
    ADAPTER_ID as ASTOR_WINES_ADAPTER_ID,
    AstorWinesAdapter,
)
from crawler.adapters.anthology import (
    ADAPTER_ID as ANTHOLOGY_ADAPTER_ID,
    AnthologyVeeziAdapter,
)
from crawler.adapters.analog_film_nyc import (
    ADAPTER_ID as ANALOG_FILM_NYC_ADAPTER_ID,
    AnalogFilmNYCAdapter,
)
from crawler.adapters.casa_italiana import (
    ADAPTER_ID as CASA_ITALIANA_ADAPTER_ID,
    CasaItalianaAdapter,
)
from crawler.adapters.carreau_club import (
    ADAPTER_ID as CARREAU_CLUB_ADAPTER_ID,
    CarreauClubFridayMeleeAdapter,
)
from crawler.adapters.brooklyn_museum import (
    ADAPTER_ID as BROOKLYN_MUSEUM_ADAPTER_ID,
    BrooklynMuseumSearchAdapter,
)
from crawler.adapters.endorphins import (
    ADAPTER_ID as ENDORPHINS_ADAPTER_ID,
    EndorphinsAdapter,
)
from crawler.adapters.gather_community import (
    ADAPTER_ID as GATHER_COMMUNITY_ADAPTER_ID,
    GatherCommunityCalendarAdapter,
)
from crawler.adapters.iic_new_york import (
    ADAPTER_ID as IIC_NEW_YORK_ADAPTER_ID,
    IICNewYorkEventsAdapter,
)
from crawler.adapters.lalliance import (
    ADAPTER_ID as LALLIANCE_ADAPTER_ID,
    LAllianceEventsAdapter,
)
from crawler.adapters.luma import (
    ADAPTER_ID as LUMA_ADAPTER_ID,
    LUMA_CATEGORY_ADAPTER_ID,
    LumaCalendarAdapter,
    LumaCategoryAdapter,
)
from crawler.adapters.metrograph import (
    ADAPTER_ID as METROGRAPH_ADAPTER_ID,
    MetrographSpecialEventsAdapter,
)
from crawler.adapters.momence_host import (
    ADAPTER_ID as MOMENCE_HOST_ADAPTER_ID,
    MomenceHostAdapter,
)
from crawler.adapters.nitehawk import ADAPTER_ID as NITEHAWK_ADAPTER_ID, NitehawkAdapter
from crawler.adapters.out_there import ADAPTER_ID, OutThereAdapter
from crawler.adapters.pioneer_works import (
    ADAPTER_ID as PIONEER_WORKS_ADAPTER_ID,
    PioneerWorksAdapter,
)
from crawler.adapters.secret_riso import (
    ADAPTER_ID as SECRET_RISO_ADAPTER_ID,
    SecretRisoCalendarAdapter,
)
from crawler.adapters.sugary import ADAPTER_ID as SUGARY_ADAPTER_ID, SugaryAdapter


ADAPTERS = {
    ADAPTER_ID: OutThereAdapter,
    ANALOG_FILM_NYC_ADAPTER_ID: AnalogFilmNYCAdapter,
    ANTHOLOGY_ADAPTER_ID: AnthologyVeeziAdapter,
    ASTOR_CENTER_ADAPTER_ID: AstorCenterAdapter,
    ASTOR_WINES_ADAPTER_ID: AstorWinesAdapter,
    BROOKLYN_MUSEUM_ADAPTER_ID: BrooklynMuseumSearchAdapter,
    CARREAU_CLUB_ADAPTER_ID: CarreauClubFridayMeleeAdapter,
    CASA_ITALIANA_ADAPTER_ID: CasaItalianaAdapter,
    ENDORPHINS_ADAPTER_ID: EndorphinsAdapter,
    GATHER_COMMUNITY_ADAPTER_ID: GatherCommunityCalendarAdapter,
    IIC_NEW_YORK_ADAPTER_ID: IICNewYorkEventsAdapter,
    LALLIANCE_ADAPTER_ID: LAllianceEventsAdapter,
    LUMA_ADAPTER_ID: LumaCalendarAdapter,
    LUMA_CATEGORY_ADAPTER_ID: LumaCategoryAdapter,
    METROGRAPH_ADAPTER_ID: MetrographSpecialEventsAdapter,
    MOMENCE_HOST_ADAPTER_ID: MomenceHostAdapter,
    NITEHAWK_ADAPTER_ID: NitehawkAdapter,
    PIONEER_WORKS_ADAPTER_ID: PioneerWorksAdapter,
    SECRET_RISO_ADAPTER_ID: SecretRisoCalendarAdapter,
    SUGARY_ADAPTER_ID: SugaryAdapter,
}


def get_adapter(adapter_id):
    try:
        return ADAPTERS[adapter_id]
    except KeyError as error:
        raise ValueError("unknown procedural adapter: {}".format(adapter_id)) from error
