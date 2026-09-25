"""Material and plant identity.

The platform and SAP do not share identities, and no exposed SAP field supplies
the mapping between them:

    material   app "500-14892"   SAP "000000000080000000"
    plant      app "PLANT-GBG"   SAP "3000"

Treating those as interchangeable is the mistake this module exists to prevent.
Every cross-initiative lookup in the frontend searches by app-side identity, so
an API that returns a bare SAP number silently breaks Material 360 and the
material router -- no error, just nothing found.

So both identities are carried side by side and neither is derived from the
other. ``sap_material_number`` is the one that is always present, because SAP is
where the data comes from; ``app_material_id`` is optional because the mapping
table does not exist yet. When VZI supplies it, it populates a field that is
already there rather than forcing a contract change.

No mapping is invented here. An absent app identity stays absent.
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MaterialIdentity(BaseModel):
    """Who a material is, in both vocabularies."""

    model_config = ConfigDict(frozen=True)

    sap_material_number: str = Field(min_length=1)
    """MARA.MATNR as SAP holds it. Never re-formatted: the extract and the OData
    projection genuinely differ (``2000000131`` vs ``000000008000000000``), and
    zero-padding is the normalise layer's job, not this contract's."""

    app_material_id: str | None = None
    """Platform-side identity, once VZI provides the mapping. ``None`` until
    then -- never a guess derived from the SAP number."""

    description: str | None = None
    """MAKT.MAKTX. Optional: description is a join away and may be missing."""

    @field_validator("sap_material_number")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("sap_material_number must not be blank")
        return value


class PlantIdentity(BaseModel):
    """Which plant, in both vocabularies."""

    model_config = ConfigDict(frozen=True)

    sap_plant_code: str = Field(min_length=1)
    """MARC.WERKS -- "1300", "1500"."""

    app_plant_id: str | None = None
    """Platform-side identity ("PLANT-BMM"), once the mapping exists."""

    name: str | None = None

    @field_validator("sap_plant_code")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("sap_plant_code must not be blank")
        return value


class PolicyVersionRef(BaseModel):
    """A pointer from a recommendation back to the policy that produced it.

    Lives with the contracts rather than with the policy package because it is
    an *identity*, not a rule: a recommendation carries one, and the policy
    package imports the contracts. Putting it the other way round makes the two
    packages import each other.
    """

    model_config = ConfigDict(frozen=True)

    policy_id: str = Field(min_length=1)
    policy_version: int = Field(ge=1)

    def __str__(self) -> str:
        return f"{self.policy_id}@v{self.policy_version}"


class MaterialPlantKey(BaseModel):
    """The grain almost everything in I07 is evaluated at.

    MRP type lives on MARC, which is per material *and* plant, so a material can
    be OAR in one plant and planned in another. Keying on the material alone
    would force an answer that does not exist.
    """

    model_config = ConfigDict(frozen=True)

    material: MaterialIdentity
    plant: PlantIdentity

    def __str__(self) -> str:
        return f"{self.material.sap_material_number}|{self.plant.sap_plant_code}"
