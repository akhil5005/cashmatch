# ---------------------------------------------------------------------------
# TLS, via CloudFront
#
# The problem this solves: there is no domain name. A certificate cannot be
# issued for a bare IP address -- not by ACM, not by Let's Encrypt, not by
# anyone, because there is no way to prove control of an address the way you
# prove control of a name. So the Elastic IP can never serve HTTPS, and the
# alternatives were all worse:
#
#   * a self-signed certificate puts an interstitial warning in front of a
#     portfolio project, which is worse than plain HTTP
#   * an ALB with ACM needs a domain *and* costs about $19 a month, none of
#     it free tier
#   * a free subdomain (DuckDNS, nip.io) plus certbot works, but makes the
#     deployment depend on a third party that can disappear, and those
#     domains are aggressively rate-limited by Let's Encrypt
#
# CloudFront hands out a trusted certificate on its own *.cloudfront.net
# name, so the domain problem disappears. Its free tier is perpetual rather
# than twelve months -- 1 TB out and 10 million requests a month -- so this
# stays free after the EC2 and RDS allowances lapse.
#
# The security win is bigger than the encryption. Once traffic arrives
# through CloudFront, the instance no longer needs to accept connections
# from the internet at all: the security group in network.tf narrows to
# CloudFront's origin-facing prefix list, and nginx rejects anything without
# a shared secret. See the comment on aws_security_group.app.
# ---------------------------------------------------------------------------

# --- the shared secret that proves a request came from *our* distribution ---
#
# The prefix list alone is necessary but not sufficient. It admits every
# CloudFront distribution in the world, including one belonging to somebody
# else who has learned the origin address -- they could point their own
# distribution at this instance and serve it as their own, bypassing every
# policy configured below. The secret closes that: CloudFront attaches it as
# a header on the origin request, nginx refuses requests that lack it, and
# the value is never visible to a browser.
resource "random_password" "origin_verify" {
  length  = 48
  special = false
}

resource "aws_ssm_parameter" "origin_verify" {
  name        = "/${var.name}/origin_verify"
  description = "Shared secret proving an origin request came from our CloudFront distribution"
  type        = "SecureString"
  value       = random_password.origin_verify.result

  tags = { Name = "${var.name}-origin-verify" }
}

# --- managed policies -------------------------------------------------------
#
# AWS maintains these; naming them is better than hand-rolling equivalents
# that then drift.

# Caches by URL, respects origin cache headers, compresses. Right for the
# hashed, immutable asset filenames Vite emits.
data "aws_cloudfront_cache_policy" "optimized" {
  name = "Managed-CachingOptimized"
}

# No caching at all. Correct for an API: a cached review queue that does not
# reflect the decision just made is worse than a slow one.
data "aws_cloudfront_cache_policy" "disabled" {
  name = "Managed-CachingDisabled"
}

# Forwards every header except Host, plus query strings and cookies. The API
# needs the real query string and method; Host must stay as the origin
# expects it.
data "aws_cloudfront_origin_request_policy" "all_except_host" {
  name = "Managed-AllViewerExceptHostHeader"
}

# --- response headers -------------------------------------------------------

resource "aws_cloudfront_response_headers_policy" "app" {
  name    = "${var.name}-app-headers"
  comment = "Security headers for the single-page review UI"

  security_headers_config {
    strict_transport_security {
      override                   = true
      access_control_max_age_sec = 31536000
      include_subdomains         = true
      preload                    = false # never preload a name you do not own
    }

    content_type_options {
      override = true # nosniff
    }

    frame_options {
      override     = true
      frame_option = "DENY"
    }

    referrer_policy {
      override        = true
      referrer_policy = "strict-origin-when-cross-origin"
    }

    # The bundle is entirely self-contained -- no CDN fonts, no analytics, no
    # external images -- so the policy can be this tight. Styles need
    # unsafe-inline because the build inlines critical CSS; scripts do not.
    content_security_policy {
      override = true
      content_security_policy = join("; ", [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'",
      ])
    }
  }
}

resource "aws_cloudfront_response_headers_policy" "api" {
  name    = "${var.name}-api-headers"
  comment = "Security headers for /api -- no CSP, because Swagger UI loads its own assets"

  security_headers_config {
    strict_transport_security {
      override                   = true
      access_control_max_age_sec = 31536000
      include_subdomains         = true
      preload                    = false
    }

    content_type_options {
      override = true
    }

    referrer_policy {
      override        = true
      referrer_policy = "strict-origin-when-cross-origin"
    }

    # Deliberately no content_security_policy and no frame_options here.
    # FastAPI's /api/docs loads Swagger UI from cdn.jsdelivr.net, and the
    # app policy above would block it. The honest fix is to vendor those
    # assets into the image; until then the documentation page gets the
    # transport and sniffing protections but not the script restrictions.
  }
}

# --- the distribution -------------------------------------------------------

resource "aws_cloudfront_distribution" "app" {
  # Gated on an account that AWS has verified for CloudFront. Everything in
  # this resource is correct and plans cleanly; what fails is the API call
  # itself, with AccessDenied and "Your account must be verified before you
  # can add new CloudFront resources". See var.enable_cdn.
  count = var.enable_cdn ? 1 : 0

  enabled         = true
  comment         = "${var.name} -- TLS termination and origin shielding"
  price_class     = "PriceClass_200" # includes India; PriceClass_All adds no useful edge here
  is_ipv6_enabled = true

  # The SPA router owns every path the API does not, so index.html has to
  # answer for unknown paths rather than CloudFront returning its own error.
  default_root_object = "index.html"

  origin {
    origin_id = "app"

    # The EIP's public DNS name rather than the address itself. CloudFront
    # wants a domain name for a custom origin, and this one is derived from
    # the Elastic IP, so it is just as stable as the address and survives the
    # instance being replaced underneath it.
    #
    # The regional form holds everywhere except us-east-1, where AWS uses
    # `compute-1.amazonaws.com` with no region segment.
    domain_name = "ec2-${replace(aws_eip.app.public_ip, ".", "-")}.${var.region}.compute.amazonaws.com"

    custom_origin_config {
      http_port  = 80
      https_port = 443
      # The origin speaks plain HTTP, and cannot speak anything else: a
      # certificate needs a name. This is the one unencrypted leg, and it is
      # why the origin is locked down by prefix list and shared secret
      # instead. Closing it properly means a domain name.
      origin_protocol_policy   = "http-only"
      origin_ssl_protocols     = ["TLSv1.2"]
      origin_read_timeout      = 60
      origin_keepalive_timeout = 5
    }

    custom_header {
      name  = "X-Origin-Verify"
      value = random_password.origin_verify.result
    }
  }

  # --- the UI ---------------------------------------------------------------
  default_cache_behavior {
    target_origin_id       = "app"
    viewer_protocol_policy = "redirect-to-https"
    compress               = true

    # Static assets. Nothing here should ever be mutated by a request.
    allowed_methods = ["GET", "HEAD", "OPTIONS"]
    cached_methods  = ["GET", "HEAD"]

    cache_policy_id            = data.aws_cloudfront_cache_policy.optimized.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.app.id
  }

  # --- the API --------------------------------------------------------------
  ordered_cache_behavior {
    path_pattern           = "/api/*"
    target_origin_id       = "app"
    viewer_protocol_policy = "redirect-to-https"
    compress               = true

    # The review actions are POST and PATCH, and uploads are POST. DELETE is
    # included because CloudFront requires the full set once any mutating
    # method is allowed -- it does not offer a subset.
    allowed_methods = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods  = ["GET", "HEAD"]

    cache_policy_id            = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id   = data.aws_cloudfront_origin_request_policy.all_except_host.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.api.id
  }

  # A single-page app: an unknown path is a client route, not a missing file.
  # nginx already does this for the origin, but CloudFront caches 404s from
  # the origin and would serve its own error page for them.
  custom_error_response {
    error_code            = 404
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 10
  }

  custom_error_response {
    error_code            = 403
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 10
  }

  viewer_certificate {
    # CloudFront's own certificate for *.cloudfront.net. The trade-off: AWS
    # pins the minimum protocol to TLSv1 for the default certificate and
    # ignores any override, so TLS 1.0 remains *available* at the edge even
    # though every current browser negotiates 1.2 or 1.3. Enforcing a 1.2
    # floor requires a custom domain and an ACM certificate -- which is the
    # same thing the whole no-domain problem is waiting on.
    cloudfront_default_certificate = true
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  tags = { Name = "${var.name}-cdn" }
}
