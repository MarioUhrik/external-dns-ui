# Copyright (c) 2020 SIGHUP s.r.l All rights reserved.
# Use of this source code is governed by a BSD-style
# license that can be found in the LICENSE file.

import os
from functools import wraps
from logging.config import dictConfig

from flask import Flask, render_template
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.config.config_exception import ConfigException
from urllib3.exceptions import MaxRetryError, NewConnectionError

from urllib.parse import urljoin

# Set up logging
dictConfig(
    {
        "version": 1,
        "formatters": {
            "default": {"format": "[%(asctime)s] %(levelname)s: %(message)s"}
        },
        "handlers": {
            "wsgi": {
                "class": "logging.StreamHandler",
                "stream": "ext://flask.logging.wsgi_errors_stream",
                "formatter": "default",
            }
        },
        "root": {
            "level": os.environ.get("EDUI_LOG_LEVEL", "INFO"),
            "handlers": ["wsgi"],
        },
    }
)

app = Flask(__name__)

# Update app config with env vars
app.config.update(
    {
        "PREFERRED_URL_SCHEME": os.environ.get("EDUI_PREFERRED_URL_SCHEME", "http"),
        "EXTERNAL_DNS_NAMESPACE": os.environ.get("EDUI_NAMESPACE", "external-dns"),
    }
)

def dict_to_li(my_dict, html):
    """Recursive function to convert dict items into <li> html tags"""
    if my_dict is None:
        return html
    for k, v in my_dict.items():
        app.logger.debug("Processing %s, %s" % (k, v))
        if not isinstance(v, dict):
            html += "<li>%s: %s</li>" % (k, v)
        else:
            html += "<li>%s: %s</li>" % (k, dict_to_li(v, html))
    html += "</ul>"
    return html


@app.template_filter("dict_to_ul")
def dict_to_ul(s):
    """
    Helper to convert recursively dict elements to an html unsorted list
    """
    app.logger.debug("Flattening %s" % s)
    result = '<ul style="padding-left:2em">'
    result = dict_to_li(s, result)
    app.logger.debug("Result of flattening: %s" % result)
    return str(result)


def get_api(api):
    """
    This function returns the corresponding API object to be used to query the API server.
    """
    config.load_incluster_config()
    if (api == "ExtensionsV1beta1Api"):
        return client.ExtensionsV1beta1Api()
    if (api == "CoreV1Api"):
        return client.CoreV1Api()
    return client.CoreV1Api()

@app.route("/health")
def health():
    """Health check endpoint for probes"""
    return {"message": "OK"}

#------------------------------------===================================================---------------------------------------------------

def filter_hostnames(hostnames, domain_filter):
    """
    This function removes hostnames that don't match the domain_filter from the input map of hostnames=>address
    """
    for hostname in hostnames.copy():
        if domain_filter not in hostname:
            hostnames.pop(hostname)
    return hostnames

def parse_hostnames_from_ingresses(ingress_list):
    """
    This function parses a list of Ingress objects into a map of hostname=>address
    """
    hostnames = {}
    for ingress in ingress_list:
        rules = ingress.spec.rules
        if ingress.status.load_balancer.ingress is None:
            continue
        address = ingress.status.load_balancer.ingress[0].ip
        for rule in rules:
            host = rule.host
            hostnames[host] = address
    return hostnames

def get_all_ingresses():
    """
    This function returns a list of ingresses from all namespaces
    """
    api = get_api("ExtensionsV1beta1Api")
    ingress_list = api.list_ingress_for_all_namespaces()
    return ingress_list.items

def get_external_dns_logs(since_seconds):
    """
    This function returns the logs of the external-dns container, but only up to since_seconds old
    """
    api = get_api("CoreV1Api")
    pod_list = api.list_namespaced_pod(namespace=app.config.get("EXTERNAL_DNS_NAMESPACE"), label_selector="app=external-dns")
    if len(pod_list.items) != 1:
        raise Exception("Failed to find exactly one pod of external-dns!")
    external_dns_pod_name = pod_list.items[0].metadata.name
    return api.read_namespaced_pod_log(namespace=app.config.get("EXTERNAL_DNS_NAMESPACE"), name=external_dns_pod_name, container="external-dns", since_seconds=since_seconds)

def get_external_dns_deployment():
    """
    This function returns the external-dns Kubernetes deployment
    """
    api = get_api("ExtensionsV1beta1Api")
    deployment_list = api.list_namespaced_deployment(namespace=app.config.get("EXTERNAL_DNS_NAMESPACE"), field_selector="metadata.name=external-dns")
    if len(deployment_list.items) != 1:
        raise Exception("Failed to find the deployment of external-dns!")
    return deployment_list.items[0]

def get_domain_filter():
    """
    This function returns the domain-filter currently in use by external-dns
    """
    external_dns_deployment = get_external_dns_deployment()
    container_args = external_dns_deployment.spec.template.spec.containers[0].args
    domain_filter_arg = list(filter(lambda x:"domain-filter" in x, container_args))[0]
    return domain_filter_arg.split("=",1)[1]

def get_hostnames():
    """
    This function returns a map of hostnames to IP addresses, as extracted from K8s Ingress objects.
    Only hostnames matching the external-dns domain-filter are considered.
    """
    all_ingresses = get_all_ingresses()
    app.logger.debug("All ingresses: " + str(all_ingresses))
    all_hostnames = parse_hostnames_from_ingresses(all_ingresses)
    app.logger.debug("All hostnames: " + str(all_hostnames))
    domain_filter = get_domain_filter()
    app.logger.debug("Domain filter: " + domain_filter)
    filtered_hostnames = filter_hostnames(all_hostnames, domain_filter)
    app.logger.debug("Filtered hostnames: " + str(filtered_hostnames))
    return filtered_hostnames

def check_dns_status(hostname, address):
    """
    This function returns true if the hostname resolves to the address
    """
    #return not bool(os.system("dig +short " + hostname + " | grep " + address))
    return not bool(os.system("getent hosts " + hostname + " | grep " + address))

def get_hostname_statuses():
    """
    This function constructs a map, where the keys are hostnames to be handled by external-dns, and the values are their DNS creation status
    """
    hostname_statuses = {}
    hostnames = get_hostnames()
    for hostname in hostnames.keys():
        hostname_statuses[hostname] = check_dns_status(hostname, hostnames[hostname])
    return hostname_statuses

def is_up_to_date():
    """
    This function returns true if external-dns reported "All records are already up to date" in the past 70 seconds
    """
    return "All records are already up to date" in get_external_dns_logs(70)

@app.route("/")
def index():
    """Readonly status page view"""
    hostname_statuses = get_hostname_statuses()
    up_to_date = is_up_to_date()
    return render_template("hostnames.html", hostname_statuses=hostname_statuses, up_to_date=up_to_date)
