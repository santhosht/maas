# Copyright 2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""MAAS OpenAPI definition.

This definition follows the rules and limitations of the ReST documentation.
(see doc.py and doc_handler.py).
"""

from inspect import getdoc
import json
from textwrap import dedent

from django.http import HttpResponse
import yaml

from maasserver.api import support
from maasserver.api.annotations import APIDocstringParser
from maasserver.api.doc import find_api_resources, generate_doc
from maasserver.djangosettings import settings
from maasserver.models.config import Config
from maasserver.models.controllerinfo import get_maas_version
from maasserver.utils import build_absolute_uri


def openapi_docs_context(request):
    """Return the aadditional context needed for the oapi doc template to function"""
    colourmap = {
        "bark": "#585841",
        "blue": "#0060bf",
        "magenta": "#974097",
        "olive": "#3d5f11",
        "prussian green": "#225d5c",
        "purple": "#7764d8",
        "red": "#a71b33",
        "sage": "#4e5f51",
        "viridian": "#025a3d",
    }
    local_server = _get_maas_servers()[0]
    context = {
        "openapi_url": local_server["url"] + "openapi.yaml",
        "maas_colour": colourmap.get(
            Config.objects.get_config("theme").lower(), "#262626"
        ),
        "maas_name": local_server["description"].removesuffix(" API"),
        "maas_version": get_maas_version(),
    }
    return context


def landing_page(request):
    """Render a landing page with pointers for the MAAS API.

    :return: An `HttpResponse` containing a JSON page with pointers to both
        human-readable documentation and api definitions.
    """
    description = get_api_landing_page()
    for link in description["resources"]:
        link["href"] = build_absolute_uri(request, link["path"])
    # Return as a JSON document
    return HttpResponse(
        json.dumps(description),
        content_type="application/json",
    )


def endpoint(request):
    """Render the OpenApi endpoint.

    :return: An `HttpResponse` containing a YAML document that complies
        with the OpenApi spec 3.0.
    """
    description = get_api_endpoint()
    # Return as a YAML document
    return HttpResponse(
        yaml.dump(description),
        content_type="application/openapi+yaml",
    )


def get_api_landing_page():
    """Return the API landing page"""
    description = {
        "title": "MAAS API",
        "description": "API landing page for MAAS",
        "resources": [
            {
                "path": "/MAAS/api",
                "rel": "self",
                "type": "application/json",
                "title": "this document",
            },
            {
                "path": f"{settings.API_URL_PREFIX}openapi.yaml",
                "rel": "service-desc",
                "type": "application/openapi+yaml",
                "title": "the API definition",
            },
            {
                "path": "/MAAS/api/docs/",
                "rel": "service-doc",
                "type": "text/html",
                "title": "the API documentation",
            },
        ],
    }
    return description


def get_api_endpoint():
    """Return the API endpoint"""
    description = {
        "openapi": "3.0.0",
        "info": {
            "title": "MAAS HTTP API",
            "description": "This is the documentation for the API that lets you control and query MAAS. You can find out more about MAAS at [https://maas.io/](https://maas.io/).",
            "version": "2.0.0",
            "license": {
                "name": "GNU Affero General Public License version 3",
                "url": "https://www.gnu.org/licenses/agpl-3.0.en.html",
            },
        },
        "paths": _render_oapi_paths(),
        "externalDocs": {
            "description": "MAAS API documentation",
            "url": "/MAAS/docs/api.html",
        },
        "servers": _get_maas_servers(),
    }
    return description


def _get_maas_servers():
    """Return a servers defintion of the public-facing MAAS address.

    :return: An object describing the MAAS public-facing server.
    """
    maas_url = (
        Config.objects.get_config("maas_url").rstrip("/").removesuffix("/MAAS")
    )
    maas_name = Config.objects.get_config("maas_name")
    return [
        {
            "url": f"{maas_url}{settings.API_URL_PREFIX}",
            "description": f"{maas_name} API",
        },
    ]


def _new_path_item(params):
    path_item = {}
    for p in params:
        path_item.setdefault("parameters", []).append(
            {
                "name": p,
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
            }
        )
    return path_item


def _render_oapi_oper_item(http_method, op, doc, uri_params, function):
    oper_id = op or support.OperationsResource.crudmap.get(http_method)
    oper_obj = {
        "operationId": f"{doc.name}_{oper_id}",
        "tags": [doc.handler.api_doc_section_name],
        "summary": f"{doc.name} {oper_id}",
        "description": dedent(doc.doc).strip(),
        "responses": {},
    }
    oper_docstring = _oapi_item_from_docstring(
        function, http_method, uri_params
    )
    # Only overwrite the values that are non-blank
    oper_obj.update({k: v for k, v in oper_docstring.items() if v})
    return oper_obj


def _oapi_item_from_docstring(function, http_method, uri_params):
    def _type_to_string(schema):
        match schema:
            case "Boolean":
                return "boolean"
            case "Float":
                return "number"
            case "Int":
                return "integer"
            case "String":
                return "string"
            case other:
                return "object"

    def _response_pair(ap_dict):
        status_code = "HTTP Status Code"
        status = content = {}
        paired = []
        for response in reversed(ap_dict["errors"] + ap_dict["successes"]):
            if response["type"] == status_code:
                status = response
                if content in paired:
                    content = {}
                paired.extend([status, content])
            else:
                content = response
        paired = iter(paired)
        return zip(paired, paired)

    oper_obj = {}
    body = {
        "type": "object",
        "additionalProperties": {"type": "string"},
    }
    ap = APIDocstringParser()
    docstring = getdoc(function)
    if docstring and ap.is_annotated_docstring(docstring):
        ap.parse(docstring)
        ap_dict = ap.get_dict()
        oper_obj["summary"] = ap_dict["description_title"].strip()
        oper_obj["description"] = ap_dict["description"].strip()
        for param in ap_dict["params"]:
            description = param["description_stripped"]
            name = param["name"].strip("}{")
            required = param["options"]["required"].lower() == "true"
            if param["name"][0] == "{":
                param_dict = {
                    "name": name,
                    "in": "path" if name in uri_params else "query",
                    "description": description,
                    "schema": {
                        "type": _type_to_string(param["type"]),
                    },
                    "required": required,
                }
                oper_obj.setdefault("parameters", []).append(param_dict)
            elif http_method.lower() in ("put", "post"):
                body.setdefault("properties", {}).update(
                    {
                        name: {
                            "description": description,
                            "type": _type_to_string(param["type"]),
                        }
                    }
                )
                if required:
                    body.setdefault("required", []).append(name)

        for (status, content) in _response_pair(ap_dict):
            response = {
                "description": content.get(
                    "description_stripped", status["description_stripped"]
                ),
            }
            match content.get("type", "").lower():
                case "content":
                    response["content"] = {
                        "text/plain": {
                            "schema": {"type": "string"},
                        },
                    }
                case "json":
                    response["content"] = {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "additionalProperties": True,
                            },
                        },
                    }

            status_code = status["name"]
            if not status_code.isdigit():
                status_code = status["description_stripped"]
            oper_obj.setdefault("responses", {}).update(
                {status_code: response},
            )

    if body.get("properties"):
        oper_obj.update(
            {
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {**body},
                        },
                    },
                }
            }
        )

    return oper_obj


def _render_oapi_paths():
    from maasserver import urls_api as urlconf

    def _resource_key(resource):
        return resource.handler.__class__.__name__

    def _export_key(export):
        (http_method, op), function = export
        return http_method, op or "", function

    resources = find_api_resources(urlconf)
    paths = {}

    for res in sorted(resources, key=_resource_key):
        handler = type(res.handler)
        doc = generate_doc(handler)
        uri = doc.resource_uri_template
        exports = handler.exports.items()
        (_, params) = doc.handler.resource_uri()

        for (http_method, op), function in sorted(exports, key=_export_key):
            oper_uri = f"{uri}op-{op}" if op else uri
            path_item = paths.setdefault(
                f"/{oper_uri.removeprefix(settings.API_URL_PREFIX)}",
                _new_path_item(params),
            )
            path_item[http_method.lower()] = _render_oapi_oper_item(
                http_method, op, doc, params, function
            )
    return paths
